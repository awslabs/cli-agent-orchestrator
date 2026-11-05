"""#1199: the engine asks the task features beside model.route and applies the local match."""

import asyncio
import dataclasses
import json
import logging
import math
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.decisions.types import (
    FEATURE_POINTS,
    DecisionAnswer,
    LaunchPlan,
)

TIERS = {
    "claude_code": {
        "small": "claude-haiku-5-5",
        "medium": "claude-sonnet-5-5",
        "large": "claude-opus-5-5",
    },
    "codex": {"small": "gpt-5.5-mini", "medium": "gpt-5.5", "large": "gpt-5.5-pro"},
}
# Synthetic labels, as #1174's engine tests use: on the real table codex is advisory.
LABELS = {"claude_code": "hard", "codex": "sandboxed"}
CATALOG = {
    "version": 1,
    "long_context_tokens": 500000,
    "models": {
        "claude_code": {
            "claude-sonnet-5-5": {"context_tokens": 200000, "cost": "medium", "good_for": []},
            "claude-opus-5-5": {
                "context_tokens": 1000000,
                "cost": "high",
                "good_for": ["deep_reasoning"],
            },
        },
        "codex": {
            "gpt-5.5": {"context_tokens": 256000, "cost": "low", "good_for": ["read_only"]},
            "gpt-5.5-pro": {"context_tokens": 2000000, "cost": "medium", "good_for": ["read_only"]},
        },
    },
}
BRIEF = "Read the whole payments service and map every caller of the charge API. Report only."
NO_FEATURES = {
    "task.long_context": "no",
    "task.deep_reasoning": "no",
    "task.read_only": "no",
    "task.needs_network": "no",
}
ALL_POINTS = frozenset({"model.route", "effort.route", "provider.route", *FEATURE_POINTS})
# The eight nullable decision_records columns #1199 adds.
MATCH_COLUMNS = (
    "match_rule",
    "match_needs",
    "match_covered",
    "match_tiebreak",
    "match_from",
    "match_pick",
    "catalog_version",
    "catalog_hash",
)


class Decider:
    version = "1"

    def __init__(self, name="feat", points=ALL_POINTS):
        self.name = name
        self.points = frozenset(points)
        self.answers = {
            "provider.route": "codex",
            "model.route": "medium",
            "effort.route": "high",
            **NO_FEATURES,
        }
        self.confidence = {}
        self.probabilities = {}
        self.requests = []
        self.gate = None

    async def decide(self, request, config):
        self.requests.append(request)
        if self.gate is not None:
            await self.gate(request)
        return {
            point: DecisionAnswer(
                self.answers[point],
                self.probabilities.get(
                    point, {self.answers[point]: self.confidence.get(point, 0.9)}
                ),
            )
            for point in request.points
            if point in self.answers
        }


def load(catalog):
    from cli_agent_orchestrator.decisions.catalog import load_catalog

    return load_catalog({"decisions": {"match": catalog}}, TIERS) if catalog is not None else None


class Harness:
    def __init__(self, tmp_path, name="h", decider=None, emit=None):
        from cli_agent_orchestrator.clients.database import DecisionRecordModel
        from cli_agent_orchestrator.decisions.store import DecisionStore

        db = create_engine("sqlite:///" + str(tmp_path / f"{name}.db"))
        DecisionRecordModel.__table__.create(db)
        self.events = []
        self.store = DecisionStore(
            sessionmaker(bind=db),
            tmp_path / "decision-hash.key",
            emit=emit if emit is not None else self.events.append,
        )
        self.decider = decider if decider is not None else Decider()
        self.extra = {}
        self.max_pending = 64
        self.configure()

    def configure(
        self,
        features="on",
        route="on",
        model="on",
        effort="on",
        route_decider=None,
        catalog=CATALOG,
        threshold=0.7,
        timeout_ms=1000,
        features_decider=None,
    ):
        from cli_agent_orchestrator.decisions.match import MATCH_DECIDER
        from cli_agent_orchestrator.decisions.settings import DecisionSettings, PointSettings
        from cli_agent_orchestrator.decisions.types import PointState

        name = self.decider.name
        points = {
            "provider.route": PointSettings(
                PointState(route), route_decider if route_decider else MATCH_DECIDER
            ),
            "model.route": PointSettings(PointState(model), name),
            "effort.route": PointSettings(PointState(effort), name),
        }
        if features is not None:
            points["task.features"] = PointSettings(PointState(features), features_decider or name)
        values = dict(
            points=points,
            model_tiers=TIERS,
            confidence_threshold=threshold,
            on_timeout_ms=timeout_ms,
        )
        if catalog is not None:
            values["match_catalog"] = load(catalog)
        self.settings = DecisionSettings(**values)
        return self

    def engine(self):
        from cli_agent_orchestrator.decisions.engine import DecisionEngine
        from cli_agent_orchestrator.decisions.registry import DeciderRegistry
        from cli_agent_orchestrator.decisions.shadow import ShadowRunner

        sources = {self.decider.name: lambda: self.decider}
        sources.update(self.extra)
        return DecisionEngine(
            DeciderRegistry(sources),
            self.store,
            ShadowRunner(self.store, max_pending=self.max_pending),
            settings_loader=lambda: self.settings,
            honors_model=lambda *args: True,
            honors_effort=lambda *args: True,
        )

    def request(
        self,
        fields=("provider.route", "model.route", "effort.route"),
        options=("claude_code", "codex"),
        labels=LABELS,
        launch_tier="medium",
        network=True,
        message=BRIEF,
        min_tier="small",
        max_tier="large",
    ):
        from cli_agent_orchestrator.decisions.engine import DelegationRequest
        from cli_agent_orchestrator.decisions.policy import PolicyBounds
        from cli_agent_orchestrator.decisions.targets import TargetKind
        from cli_agent_orchestrator.decisions.types import ProviderOptions

        return DelegationRequest(
            "assign",
            "claude_code",
            "Ramones-code_reading-3f9a",
            None,
            message,
            target_kind=TargetKind.EPHEMERAL,
            field_states={point: "auto" for point in fields},
            policy=PolicyBounds(
                min_tier=min_tier,
                default_tier=launch_tier,
                max_tier=max_tier,
                min_effort="low",
                default_effort="medium",
                max_effort="high",
                allowed_providers=frozenset(TIERS),
            ),
            purpose="code_reading",
            description="Reads a large codebase end to end and maps call paths",
            provider_options=(
                ProviderOptions(options, {p: labels[p] for p in options})
                if "provider.route" in fields
                else None
            ),
            launch_tier=launch_tier,
            network_granted=network,
        )

    async def launch(self, req=None, *, drain=True):
        engine = self.engine()
        plan = await engine.prepare_launch(req if req is not None else self.request())
        if drain:
            await engine.runner.drain()
        self.last_engine = engine
        return plan

    def rows(self, point=None):
        return [
            row
            for row in sorted(self.store.list(), key=lambda row: row["id"])
            if point is None or row["point"] == point
        ]

    def only(self, point):
        (row,) = self.rows(point)
        return row

    def requests(self, points=None):
        return [r for r in self.decider.requests if points is None or r.points == points]


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def plan_values(plan):
    return None if plan is None else (plan.model, plan.provider, plan.effort, plan.hints)


def features(h, **answers):
    for name, answer in answers.items():
        h.decider.answers["task." + name] = answer


# AC1: off by default.


@pytest.mark.asyncio
async def test_AC1_off_by_default_asks_no_feature_question(h):
    from cli_agent_orchestrator.decisions.settings import DecisionSettings

    assert DecisionSettings().points["task.features"].state.value == "off"
    h.configure(features="off", route_decider="feat")
    features(h, long_context="yes", read_only="yes")
    await h.launch()
    assert not h.requests(FEATURE_POINTS)
    assert all(not row["point"].startswith("task.") for row in h.rows())
    assert all(row["match_rule"] is None for row in h.rows())


def test_AC1_plans_and_records_match_1174_byte_for_byte(tmp_path):
    from test.decisions import _ac1_1174_scenarios as scenarios

    golden = json.loads(
        (
            Path(__file__).resolve().parents[1] / "fixtures/decisions_1174_ac1_golden.json"
        ).read_text()
    )
    result = scenarios.run(tmp_path, with_new_columns=True)
    # This PR's columns are NULL on every one of those rows (11 rows in 6 scenarios). Only
    # they are checked: a column another change adds to main is that change's to test.
    new_rows = [row for scenario in result.values() for row in scenario.pop("new_columns")]
    assert len(new_rows) == sum(len(scenario["rows"]) for scenario in result.values()) == 11
    assert all(all(row[c] is None for c in MATCH_COLUMNS) for row in new_rows)
    assert json.loads(json.dumps(result, default=list)) == golden
    from cli_agent_orchestrator.clients.database import DecisionRecordModel

    added = scenarios.new_columns(tmp_path)
    assert set(MATCH_COLUMNS) <= added, set(MATCH_COLUMNS) - added
    assert all(DecisionRecordModel.__table__.columns[c].nullable for c in MATCH_COLUMNS)


# AC2: nothing new leaves.


@pytest.mark.asyncio
async def test_AC2_feature_request_equals_model_route_request_but_points(h):
    h.configure(route="off")
    await h.launch(h.request(fields=("model.route", "effort.route")))
    (feature,) = h.requests(FEATURE_POINTS)
    (model,) = h.requests(("model.route",))
    a, b = dataclasses.asdict(feature), dataclasses.asdict(model)
    assert a.pop("points") == FEATURE_POINTS
    assert b.pop("points") == ("model.route",)
    assert a == b
    assert feature.options is None and feature.request_id == model.request_id


# A second catalog for the same model_tiers and the same reachable model ids, with every
# declared fact different: costs (a different set of cost words), windows, strengths, the
# long_context threshold, and one extra unreachable declaration. The needs still pick the same
# CLI and tier, so any difference in what the deciders are sent can only be the catalog leaking.
CATALOG_B = {
    "version": 1,
    "long_context_tokens": 800000,
    "models": {
        "claude_code": {
            "claude-sonnet-5-5": {
                "context_tokens": 150000,
                "cost": "high",
                "good_for": ["deep_reasoning"],
            },
            "claude-opus-5-5": {"context_tokens": 900000, "cost": "medium", "good_for": []},
        },
        "codex": {
            "gpt-5.5": {
                "context_tokens": 300000,
                "cost": "medium",
                "good_for": ["read_only", "deep_reasoning"],
            },
            "gpt-5.5-pro": {"context_tokens": 3000000, "cost": "high", "good_for": ["read_only"]},
            "gpt-old": {"context_tokens": 7777777, "cost": "high", "good_for": ["read_only"]},
        },
    },
}


def _strings(value):
    """Every string inside a request's asdict, keys and values, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


@pytest.mark.asyncio
async def test_AC2_no_catalog_fact_in_any_request(tmp_path):
    import re

    from cli_agent_orchestrator.decisions.catalog import COSTS

    sent, plans = {}, {}
    for name, catalog in (("a", CATALOG), ("b", CATALOG_B)):
        h = Harness(tmp_path, name)
        h.configure(catalog=catalog)
        features(h, long_context="yes", read_only="yes")
        plans[name] = plan_values(await h.launch())
        sent[name] = [
            {k: v for k, v in dataclasses.asdict(r).items() if k != "request_id"}
            for r in h.decider.requests
        ]
    # The same launch under both catalogs: the comparison below is about the requests only.
    assert plans["a"] == plans["b"] == ("gpt-5.5-pro", "codex", "high", ())
    asked = {tuple(r["points"]) for r in sent["a"]}
    assert asked == {FEATURE_POINTS, ("model.route",), ("effort.route",)}
    # Every request to every point is identical whatever the catalog declares.
    assert sent["a"] == sent["b"]
    words = {
        w for r in sent["a"] for text in _strings(r) for w in re.findall(r"[a-z0-9]+", text.lower())
    }
    assert words.isdisjoint(COSTS), words & set(COSTS)
    text = json.dumps(sent["a"], default=list)
    for catalog in (CATALOG, CATALOG_B):
        assert str(catalog["long_context_tokens"]) not in text
        for entries in catalog["models"].values():
            for model, facts in entries.items():
                assert model not in text
                assert str(facts["context_tokens"]) not in text
    for key in ("context_tokens", "cost", "good_for", "long_context_tokens", "match"):
        assert key not in words


@pytest.mark.asyncio
async def test_AC2_match_builds_no_http_client(h, monkeypatch):
    import httpx

    built = []
    for cls in (httpx.Client, httpx.AsyncClient):
        original = cls.__init__

        def spy(self, *args, _original=original, _cls=cls, **kwargs):
            built.append((_cls.__name__, kwargs.get("trust_env"), kwargs.get("follow_redirects")))
            _original(self, *args, **kwargs)

        monkeypatch.setattr(cls, "__init__", spy)
    features(h, long_context="yes", read_only="yes")
    plan = await h.launch()
    assert plan.provider == "codex"
    assert built == []


# AC3 and AC4: answers are validated and thresholded.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,option,probabilities",
    [
        ("maybe", "maybe", {"maybe": 0.9}),
        ("foreign_key", "yes", {"yes": 0.9, "perhaps": 0.1}),
        ("above_one", "yes", {"yes": 1.2}),
        ("negative", "yes", {"yes": 0.9, "no": -0.1}),
        ("nan", "yes", {"yes": math.nan}),
        ("bool", "yes", {"yes": True}),
        ("missing_own_key", "yes", {"no": 0.9}),
    ],
)
async def test_AC3_invalid_feature_answer_is_invalid_answer_and_not_a_need(
    h, case, option, probabilities
):
    features(h, long_context=option, read_only="yes")
    h.decider.probabilities["task.long_context"] = probabilities
    await h.launch()
    row = h.only("task.long_context")
    assert (row["outcome"], row["reason"], row["answer"]) == ("fallback", "invalid_answer", None)
    route = h.only("provider.route")
    assert route["match_needs"] == ["read_only"]
    # The other features are judged on their own.
    assert h.only("task.read_only")["outcome"] == "applied"


@pytest.mark.asyncio
async def test_AC4_yes_below_threshold_is_low_confidence_and_not_a_need(h):
    features(h, long_context="yes")
    h.decider.confidence["task.long_context"] = 0.65
    plan = await h.launch()
    row = h.only("task.long_context")
    assert (row["outcome"], row["reason"], row["answer"], row["confidence"]) == (
        "fallback",
        "low_confidence",
        "yes",
        0.65,
    )
    assert (h.only("provider.route")["match_rule"], h.only("model.route")["match_rule"]) == (
        "none_needed",
        "none_needed",
    )
    assert plan.provider is None and plan.model == "claude-sonnet-5-5"


# AC5: no need, no change (property test against the plan with task.features off).


@st.composite
def no_need_cases(draw):
    ceiling = draw(st.sampled_from(("small", "medium", "large")))
    tiers = ("small", "medium", "large")
    floor = draw(st.sampled_from(tiers[: tiers.index(ceiling) + 1]))
    tier = draw(st.sampled_from(tiers[tiers.index(floor) : tiers.index(ceiling) + 1]))
    answers = {point: draw(st.sampled_from(("no", "unsure"))) for point in FEATURE_POINTS}
    confidence = {point: draw(st.floats(0.0, 1.0)) for point in FEATURE_POINTS}
    return dict(
        floor=floor,
        ceiling=ceiling,
        tier=tier,
        size=draw(st.sampled_from(tiers + ("unsure",))),
        size_confidence=draw(st.floats(0.0, 1.0)),
        route=draw(st.sampled_from(("on", "shadow", "off"))),
        model=draw(st.sampled_from(("on", "off"))),
        options=draw(st.sampled_from((("claude_code",), ("claude_code", "codex")))),
        provider_auto=draw(st.booleans()),
        answers=answers,
        confidence=confidence,
        threshold=draw(st.floats(0.0, 1.0)),
    )


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(case=no_need_cases())
def test_AC5_no_need_plan_equals_off_plan(tmp_path_factory, case):
    directory = tmp_path_factory.mktemp("ac5")
    fields = ("model.route", "effort.route") + (
        ("provider.route",) if case["provider_auto"] else ()
    )
    plans, rows = {}, {}
    for mode in ("off", "on"):
        h = Harness(directory, mode)
        h.decider.answers.update(case["answers"], **{"model.route": case["size"]})
        h.decider.confidence.update(case["confidence"], **{"model.route": case["size_confidence"]})
        h.configure(
            features=mode, route=case["route"], model=case["model"], threshold=case["threshold"]
        )
        req = h.request(
            fields=fields,
            options=case["options"],
            launch_tier=case["tier"],
            min_tier=case["floor"],
            max_tier=case["ceiling"],
        )
        plans[mode] = asyncio.run(h.launch(req))
        rows[mode] = h.rows()
    assert plan_values(plans["on"]) == plan_values(plans["off"])
    for row in rows["on"]:
        if row["match_rule"] is not None:
            assert row["match_rule"] == "none_needed"
            assert row["match_pick"] is None


# AC6: only the options.


@pytest.mark.asyncio
async def test_AC6_only_the_options_codex_never_appears(h):
    only_codex_covers = {
        "version": 1,
        "models": {
            "claude_code": {
                "claude-sonnet-5-5": {"context_tokens": 200000, "cost": "medium"},
                "claude-opus-5-5": {"context_tokens": 200000, "cost": "high"},
            },
            "codex": {"gpt-5.5-pro": {"context_tokens": 2000000, "cost": "low"}},
        },
    }
    h.configure(catalog=only_codex_covers)
    features(h, long_context="yes")
    plan = await h.launch(h.request(options=("claude_code",)))
    assert plan.provider is None and plan.model == "claude-sonnet-5-5"
    route = h.only("provider.route")
    assert route["reason"] == "single_option"
    model = h.only("model.route")
    assert (model["match_rule"], model["match_pick"], model["applied_value"]) == (
        "no_cover",
        None,
        "medium",
    )
    text = json.dumps(
        [{k: v for k, v in row.items() if k != "catalog_hash"} for row in h.rows()], default=str
    )
    assert "codex" not in text and "gpt-" not in text
    assert "codex" not in json.dumps(plan_values(plan))
    sent = json.dumps([dataclasses.asdict(r) for r in h.decider.requests], default=list)
    assert "codex" not in sent


# #810, as amended on 2026-11-05: a decider can never raise a helper's tier, providers or tools.
# CAO's own match may raise the tier, only to a model the operator declared and only up to
# ephemeral.max_tier, and never raises providers or tools. Here codex covers every need better,
# and provider.route isn't the match's: the tier match stays on the creator's CLI.
UNDECLARED_OPUS = {
    "version": 1,
    "models": {
        "claude_code": {"claude-sonnet-5-5": {"context_tokens": 200000, "cost": "medium"}},
        "codex": CATALOG["models"]["codex"],
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route,max_tier,catalog,tier",
    [
        ("off", "large", CATALOG, "large"),
        ("off", "medium", CATALOG, "medium"),
        ("off", "large", UNDECLARED_OPUS, "medium"),
        ("on", "large", CATALOG, "large"),
        ("on", "medium", CATALOG, "medium"),
    ],
    ids=["raised", "max_tier_medium", "opus_undeclared", "decider_route", "decider_max_medium"],
)
async def test_810_the_match_raises_only_the_tier_on_the_helpers_cli(
    h, route, max_tier, catalog, tier
):
    # "on" binds provider.route to the third-party decider, which answers the creator's CLI.
    h.configure(route=route, route_decider="feat", catalog=catalog)
    h.decider.answers["provider.route"] = "claude_code"
    features(h, long_context="yes", read_only="yes")
    plan = await h.launch(h.request(max_tier=max_tier))
    # Never another CLI: the plan names none, and every record ran on the creator's.
    assert plan.provider is None
    assert {row["provider"] for row in h.rows()} == {"claude_code"}
    assert all(row["decider"] != "match" for row in h.rows("provider.route"))
    # The tier only, up to max_tier, and only to a declared model.
    model = h.only("model.route")
    assert (model["answer"], model["applied_value"]) == ("medium", tier)
    assert plan.model == TIERS["claude_code"][tier]
    if tier != "medium":
        assert plan.model in catalog["models"]["claude_code"]
        assert model["match_pick"] == f"claude_code/{tier}"
    else:
        assert model["match_rule"] == "no_cover" and model["match_pick"] is None
    # Never tools: a plan carries none, and the request's network grant is only read.
    assert [f.name for f in dataclasses.fields(LaunchPlan) if "tool" in f.name] == []
    assert plan.hints == ()


# The CLI half: provider.route answered by match.


@pytest.mark.asyncio
async def test_a_match_moves_the_cli_and_raises_the_tier_and_records_both_rows(h):
    features(h, long_context="yes", read_only="yes")
    plan = await h.launch()
    assert plan_values(plan) == ("gpt-5.5-pro", "codex", "high", ())
    route = h.only("provider.route")
    assert (route["decider"], route["decider_version"], route["state"]) == (
        "match",
        "match-1",
        "on",
    )
    assert (route["answer"], route["applied_value"], route["outcome"], route["reason"]) == (
        "codex",
        "codex",
        "applied",
        None,
    )
    assert route["probabilities"] == {"codex": 1.0} and route["confidence"] == 1.0
    assert (route["provider"], route["fallback_value"]) == ("codex", "claude_code")
    assert route["options"] == ["claude_code", "codex"]
    assert (route["fallback_enforcement"], route["applied_enforcement"]) == ("hard", "sandboxed")
    assert (route["match_rule"], route["match_needs"], route["match_covered"]) == (
        "covered",
        ["long_context", "read_only"],
        ["long_context", "read_only"],
    )
    assert (route["match_tiebreak"], route["match_pick"], route["catalog_version"]) == (
        "only",
        "codex/large",
        1,
    )
    assert route["catalog_hash"] and route["match_from"] is None
    model = h.only("model.route")
    assert (model["provider"], model["answer"], model["candidate_value"]) == (
        "codex",
        "medium",
        "medium",
    )
    assert (model["applied_value"], model["candidate_model"], model["match_from"]) == (
        "large",
        "gpt-5.5-pro",
        "medium",
    )
    assert (model["match_rule"], model["match_pick"]) == ("covered", "codex/large")
    assert model["catalog_hash"] == route["catalog_hash"]
    # effort.route runs on the matched CLI, after the match.
    (effort,) = h.requests(("effort.route",))
    assert effort.facts.provider == "codex"
    assert h.only("effort.route")["provider"] == "codex"
    # Features and size are asked on the creator's CLI, before the match.
    (feature,) = h.requests(FEATURE_POINTS)
    assert feature.facts.provider == "claude_code"
    for point in FEATURE_POINTS:
        row = h.only(point)
        assert (row["provider"], row["fallback_source"], row["decider"]) == (
            "claude_code",
            "none",
            "feat",
        )
    assert h.only("task.long_context")["applied_value"] == "yes"


@pytest.mark.asyncio
async def test_a_tie_on_cover_takes_the_cheaper_then_stays_on_the_creators_cli(h):
    # Only long_context is needed; claude_code/large (high) and codex/large (medium) cover it.
    features(h, long_context="yes")
    plan = await h.launch()
    assert (plan.provider, plan.model) == ("codex", "gpt-5.5-pro")
    assert h.only("provider.route")["match_tiebreak"] == "cost"


@pytest.mark.asyncio
async def test_provider_route_with_match_and_features_off_records_no_features(h):
    h.configure(features="off")
    features(h, long_context="yes")
    plan = await h.launch()
    assert plan.provider is None
    route = h.only("provider.route")
    assert (route["decider"], route["outcome"], route["reason"], route["match_rule"]) == (
        "match",
        "fallback",
        "no_match",
        "no_features",
    )
    assert route["catalog_version"] == 1 and route["match_pick"] is None
    assert not h.requests(FEATURE_POINTS)
    assert h.only("model.route")["match_rule"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "error", "no_answer"])
async def test_a_feature_failure_runs_the_default_and_names_the_reason(h, failure):
    async def fail(request):
        if request.points == FEATURE_POINTS:
            if failure == "timeout":
                await asyncio.sleep(10)
            if failure == "error":
                raise RuntimeError("synthetic")

    h.decider.gate = fail
    h.configure(timeout_ms=50)
    if failure == "no_answer":
        for point in FEATURE_POINTS:
            del h.decider.answers[point]
    plan = await h.launch()
    assert plan.provider is None and plan.model == "claude-sonnet-5-5"
    for point in FEATURE_POINTS:
        assert (h.only(point)["outcome"], h.only(point)["reason"]) == ("fallback", failure)
    assert h.only("provider.route")["match_rule"] == "no_features"
    assert h.only("model.route")["match_rule"] == "no_features"


@pytest.mark.asyncio
async def test_a_decider_declaring_three_features_never_gets_the_group(h, tmp_path):
    three = Decider("three", {"model.route", "effort.route", *FEATURE_POINTS[:3]})
    h.extra = {"three": lambda: three}
    h.configure(features_decider="three")
    plan = await h.launch()
    assert three.requests == []
    for point in FEATURE_POINTS:
        assert (h.only(point)["reason"], h.only(point)["decider"]) == (
            "decider_unavailable",
            "three",
        )
    assert h.only("provider.route")["match_rule"] == "no_features"
    assert plan.provider is None


@pytest.mark.asyncio
async def test_a_package_registered_as_match_is_never_asked(h):
    impostor = Decider("match")
    h.extra = {"match": lambda: impostor}
    features(h, long_context="yes", read_only="yes")
    h.configure(features_decider="match")
    await h.launch()
    assert impostor.requests == []
    assert h.only("task.read_only")["reason"] == "decider_unavailable"
    # provider.route with decider match is the built-in matcher, never the package.
    h2 = Harness(Path(h.store.key_path).parent, "h2")
    h2.extra = {"match": lambda: impostor}
    h2.configure(model="on")
    h2.decider.answers.update({"model.route": "medium"})
    await h2.launch(h2.request())
    assert impostor.requests == []


@pytest.mark.asyncio
async def test_Q5_no_raise_without_a_usable_size_answer_but_the_cli_pick_runs(h):
    features(h, long_context="yes", read_only="yes")
    h.decider.answers["model.route"] = "unsure"
    plan = await h.launch()
    # The band is the launch's own tier: codex/medium (gpt-5.5, 256k) covers read_only only.
    assert (plan.provider, plan.model) == ("codex", "gpt-5.5")
    route = h.only("provider.route")
    assert (route["match_pick"], route["match_covered"]) == ("codex/medium", ["read_only"])
    model = h.only("model.route")
    assert (model["outcome"], model["reason"], model["applied_value"]) == (
        "fallback",
        "unsure",
        None,
    )
    assert (model["match_from"], model["match_pick"]) == ("medium", "codex/medium")


@pytest.mark.asyncio
async def test_provider_route_in_shadow_records_the_full_would_be_match(h):
    h.configure(route="shadow")
    features(h, long_context="yes", read_only="yes")
    plan = await h.launch()
    # Applied: the tier match on the creator's CLI (opus covers long_context).
    assert (plan.provider, plan.model) == (None, "claude-opus-5-5")
    route = h.only("provider.route")
    assert (route["outcome"], route["answer"], route["applied_value"]) == ("shadow", "codex", None)
    assert (route["match_pick"], route["candidate_value"]) == ("codex/large", "codex")
    model = h.only("model.route")
    assert (model["match_pick"], model["applied_value"]) == ("claude_code/large", "large")


@pytest.mark.asyncio
async def test_a_mixed_advisory_pick_is_rejected_defensively(h):
    features(h, long_context="yes", read_only="yes")
    plan = await h.launch(h.request(labels={"claude_code": "hard", "codex": "advisory"}))
    assert plan.provider is None and plan.model == "claude-sonnet-5-5"
    route = h.only("provider.route")
    assert (route["outcome"], route["reason"], route["match_rule"], route["match_pick"]) == (
        "fallback",
        "no_match",
        "rejected",
        None,
    )
    assert h.only("model.route")["applied_value"] == "medium"


@pytest.mark.asyncio
async def test_a_third_party_route_decider_routes_first_then_the_tier_matches(h):
    h.configure(route_decider="feat")
    features(h, long_context="yes")
    plan = await h.launch()
    # The decider picked codex (#1174's flow); the match then raised the tier on codex.
    assert (plan.provider, plan.model) == ("codex", "gpt-5.5-pro")
    route = h.only("provider.route")
    assert (route["decider"], route["match_rule"]) == ("feat", None)
    model = h.only("model.route")
    assert (model["match_pick"], model["match_tiebreak"]) == ("codex/large", "only")


# Parallelism, failure coupling and fatal exceptions.


@pytest.mark.asyncio
async def test_features_are_asked_beside_model_route_not_after_it(h):
    started = {"features": asyncio.Event(), "model": asyncio.Event()}

    async def gate(request):
        if request.points == FEATURE_POINTS:
            started["features"].set()
            await asyncio.wait_for(started["model"].wait(), 2)
        elif request.points == ("model.route",):
            started["model"].set()
            await asyncio.wait_for(started["features"].wait(), 2)

    h.decider.gate = gate
    h.configure(route="off", timeout_ms=3000)
    features(h, long_context="yes")
    plan = await h.launch(h.request(fields=("model.route",)))
    assert h.only("model.route")["outcome"] == "applied"
    assert h.only("task.long_context")["outcome"] == "applied"
    assert plan.model == "claude-opus-5-5"


@pytest.mark.parametrize("fatal", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("when", ["decide", "cleanup", "owner_cancel"])
def test_a_fatal_feature_exception_reaches_the_owner_and_binds(h, fatal, when):
    observed = []

    async def exercise():
        started = asyncio.Event()

        async def gate(request):
            if request.points != FEATURE_POINTS:
                await asyncio.Event().wait()  # model.route waits; the features fail first
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
                await h.engine().prepare_launch(h.request(fields=("model.route",)))
            except BaseException as exc:
                observed.append(exc)

        h.decider.gate = gate
        h.configure(route="off", timeout_ms=300)
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
    assert {row["launch_status"] for row in h.rows()} <= {"not_launched"}


@pytest.mark.asyncio
async def test_cancelling_the_launch_cancels_the_feature_ask(h):
    cancelled = asyncio.Event()

    async def gate(request):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if request.points == FEATURE_POINTS:
                cancelled.set()
            raise

    h.decider.gate = gate
    h.configure(timeout_ms=5000)
    task = asyncio.ensure_future(h.launch(drain=False))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


# AC11: a bad catalog fails to today.


@pytest.mark.asyncio
async def test_AC11_version_2_is_catalog_invalid_on_every_launch(h):
    h.configure(catalog={"version": 2, "models": CATALOG["models"]})
    features(h, long_context="yes", read_only="yes")
    plans = [await h.launch(), await h.launch()]
    assert [plan_values(p) for p in plans] == [("claude-sonnet-5-5", None, "high", ())] * 2
    for row in h.rows("provider.route") + h.rows("model.route"):
        assert (row["match_rule"], row["catalog_version"], row["catalog_hash"]) == (
            "catalog_invalid",
            None,
            None,
        )
        assert row["match_needs"] == [] and row["match_pick"] is None


# AC12: no free text reaches a match column, log, span or metric.


@pytest.mark.asyncio
async def test_AC12_canary_absent_from_match_columns_logs_spans_metrics(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from cli_agent_orchestrator.decisions import telemetry

    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    spans = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    monkeypatch.setattr(telemetry.trace, "get_tracer", lambda *args: tracer.get_tracer("t"))
    monkeypatch.setattr(telemetry.metrics, "get_meter", lambda *args: meters.get_meter("t"))
    canary = "CANARY-1199-task-text"
    h = Harness(tmp_path, emit=telemetry.emit_record)
    features(h, long_context="yes", read_only="yes")
    caplog.set_level(logging.DEBUG)
    plan = await h.launch(h.request(message=f"{canary} {BRIEF}"))
    h.last_engine.bind_launch(plan, terminal_id="t1", model=plan.model, honored=True)
    rows = h.rows()
    assert any(row["match_rule"] == "covered" for row in rows)
    for row in rows:
        for column in [c for c in row if c.startswith("match_") or c.startswith("catalog_")]:
            assert canary not in json.dumps(row[column])
    assert canary not in caplog.text
    exported = spans.get_finished_spans()
    assert exported and any(
        span.attributes.get("cao.decision.match_rule") == "covered" for span in exported
    )
    assert canary not in json.dumps([dict(span.attributes) for span in exported], default=str)
    metrics = reader.get_metrics_data().to_json()
    assert "cao.decision.match_rule" in metrics and canary not in metrics


# AC15: shadow changes nothing, and records the would-be match.


@pytest.mark.asyncio
async def test_AC15_shadow_launch_equals_off_and_records_the_would_be_match(tmp_path):
    plans = {}
    harnesses = {}
    for mode in ("off", "shadow"):
        h = Harness(tmp_path, mode)
        features(h, long_context="yes", read_only="yes")
        h.configure(features=mode)
        plans[mode] = await h.launch()
        harnesses[mode] = h
    assert plan_values(plans["shadow"]) == plan_values(plans["off"])
    assert plan_values(plans["off"]) == ("claude-sonnet-5-5", None, "high", ())
    h = harnesses["shadow"]
    for point in FEATURE_POINTS:
        row = h.only(point)
        assert (row["state"], row["outcome"], row["decision_status"]) == (
            "shadow",
            "shadow",
            "done",
        )
        assert row["applied_value"] is None
    assert h.only("task.long_context")["answer"] == "yes"
    route = h.only("provider.route")
    # The launch-time columns are the off launch's; the would-be match was added later.
    assert (route["outcome"], route["reason"], route["answer"], route["applied_value"]) == (
        "fallback",
        "no_match",
        None,
        None,
    )
    assert (route["match_rule"], route["match_pick"], route["match_tiebreak"]) == (
        "covered",
        "codex/large",
        "only",
    )
    model = h.only("model.route")
    assert (model["applied_value"], model["candidate_model"]) == ("medium", "claude-sonnet-5-5")
    assert (model["match_from"], model["match_pick"]) == ("medium", "codex/large")


@pytest.mark.asyncio
async def test_a_dropped_shadow_ask_writes_no_features_to_its_targets(h):
    h.max_pending = 0
    h.configure(features="shadow")
    features(h, long_context="yes")
    await h.launch()
    for point in FEATURE_POINTS:
        assert (h.only(point)["reason"], h.only(point)["decision_status"]) == (
            "shadow_dropped",
            "dropped",
        )
    assert h.only("provider.route")["match_rule"] == "no_features"
    assert h.only("model.route")["match_rule"] == "no_features"


@pytest.mark.asyncio
async def test_an_unavailable_shadow_decider_writes_no_features(h):
    h.configure(features="shadow", features_decider="absent")
    await h.launch()
    for point in FEATURE_POINTS:
        assert (h.only(point)["reason"], h.only(point)["decision_status"]) == (
            "decider_unavailable",
            "done",
        )
    assert h.only("provider.route")["match_rule"] == "no_features"


# AC17: a raise is visible.


@pytest.mark.asyncio
async def test_AC17_raise_shows_answer_medium_applied_large_from_medium_and_the_note(h):
    h.configure(route="off")
    features(h, long_context="yes")
    plan = await h.launch(h.request(fields=("model.route", "effort.route")))
    assert plan.model == "claude-opus-5-5" and plan.provider is None
    model = h.only("model.route")
    assert (model["answer"], model["applied_value"], model["match_from"]) == (
        "medium",
        "large",
        "medium",
    )
    assert (model["outcome"], model["candidate_value"]) == ("applied", "medium")
    h.last_engine.bind_launch(plan, terminal_id="t1", model=plan.model, honored=True)
    note = h.last_engine.launch_note(plan)
    assert note == "ran on claude-opus-5-5 (auto: medium 0.90, matched long_context -> large)"
    note.encode("latin-1")  # it travels in an HTTP header


@pytest.mark.asyncio
async def test_the_note_names_the_cli_when_the_match_moved_it(h):
    features(h, long_context="yes", read_only="yes")
    plan = await h.launch()
    h.last_engine.bind_launch(plan, terminal_id="t1", model=plan.model, honored=True)
    assert h.last_engine.launch_note(plan) == (
        "ran on gpt-5.5-pro (auto: medium 0.90, matched long_context+read_only -> codex large)"
    )


# AC18: network is advisory.


@pytest.mark.asyncio
async def test_AC18_needs_network_adds_only_the_hint(tmp_path):
    plans = {}
    for case, (answer, granted) in {
        "hint": ("yes", False),
        "granted": ("yes", True),
        "no": ("no", False),
    }.items():
        h = Harness(tmp_path, case)
        features(h, needs_network=answer)
        plans[case] = (await h.launch(h.request(network=granted)), h)
    plan, h = plans["hint"]
    assert plan.hints == ("needs_network",)
    h.last_engine.bind_launch(plan, terminal_id="t1", model=plan.model, honored=True)
    note = h.last_engine.launch_note(plan)
    assert note.endswith("; the task may need network access, and this helper has no web_fetch")
    assert plans["granted"][0].hints == () and plans["no"][0].hints == ()
    base = plan_values(plans["no"][0])[:3]
    assert plan_values(plan)[:3] == base == plan_values(plans["granted"][0])[:3]
    # needs_network is never a need, so it never routes.
    assert h.only("provider.route")["match_rule"] == "none_needed"


@pytest.mark.asyncio
async def test_the_hint_alone_is_the_note_when_nothing_else_renders(h):
    h.configure(route="off", model="off", effort="off")
    features(h, needs_network="yes")
    plan = await h.launch(h.request(fields=("model.route",), network=False))
    assert h.last_engine.launch_note(plan) == (
        "the task may need network access, and this helper has no web_fetch"
    )


@pytest.mark.asyncio
async def test_shadow_features_never_add_a_hint(h):
    h.configure(features="shadow")
    features(h, needs_network="yes")
    plan = await h.launch(h.request(network=False))
    assert plan.hints == ()


@pytest.mark.asyncio
async def test_shadow_note_never_claims_a_match(tmp_path):
    h = Harness(tmp_path)
    h.configure(features="shadow", route="off")
    features(h, long_context="yes")
    plan = await h.launch(h.request(fields=("model.route", "effort.route")), drain=False)
    h.last_engine.bind_launch(plan, terminal_id="t1", model=plan.model, honored=True)
    before = h.last_engine.launch_note(plan)
    await h.last_engine.runner.drain()
    after = h.last_engine.launch_note(plan)
    assert plan.model == "claude-sonnet-5-5"
    # Without the fix, after adds ", matched long_context -> large".
    assert before == after == "ran on claude-sonnet-5-5 (auto: medium 0.90)"


@pytest.mark.asyncio
async def test_shadow_note_never_claims_a_match_with_provider_route_bound_to_match(tmp_path):
    h = Harness(tmp_path)
    h.configure(features="shadow")
    features(h, long_context="yes", read_only="yes")
    plan = await h.launch(drain=False)
    h.last_engine.bind_launch(plan, terminal_id="t1", model=plan.model, honored=True)
    before = h.last_engine.launch_note(plan)
    await h.last_engine.runner.drain()
    after = h.last_engine.launch_note(plan)
    assert (plan.provider, plan.model) == (None, "claude-sonnet-5-5")
    # The would-be match is still recorded on the rows `on` would have matched.
    for point in ("provider.route", "model.route"):
        assert (h.only(point)["match_rule"], h.only(point)["match_pick"]) == (
            "covered",
            "codex/large",
        )
    assert before == after == "ran on claude-sonnet-5-5 (auto: medium 0.90)"


# The features gate, the match's size and the reserved name.


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["on", "shadow"])
async def test_an_installed_launch_never_asks_the_features(h, state):
    from cli_agent_orchestrator.decisions.engine import DelegationRequest
    from cli_agent_orchestrator.decisions.targets import TargetKind

    h.configure(features=state)
    features(h, long_context="yes", needs_network="yes")
    # An installed launch without a model asks model.route only (docs: "Only ephemeral
    # launches ask the features").
    req = DelegationRequest(
        "assign", "claude_code", None, None, BRIEF, target_kind=TargetKind.INSTALLED
    )
    plan = await h.launch(req)
    assert [request.points for request in h.decider.requests] == [("model.route",)]
    assert [row["point"] for row in h.rows()] == ["model.route"]
    model = h.only("model.route")
    assert (model["outcome"], model["match_rule"], model["match_from"]) == ("applied", None, None)
    assert plan.model == "claude-sonnet-5-5" and plan.hints == ()


@pytest.mark.asyncio
async def test_the_match_starts_at_the_deciders_size_not_the_launch_tier(h):
    # Here sonnet covers long_context too and is cheaper: a band from the launch's own
    # medium tier would pick it, below the decider's large.
    catalog = json.loads(json.dumps(CATALOG))
    catalog["models"]["claude_code"]["claude-sonnet-5-5"]["context_tokens"] = 600000
    h.configure(route="off", catalog=catalog)
    h.decider.answers["model.route"] = "large"
    features(h, long_context="yes")
    plan = await h.launch(h.request(fields=("model.route", "effort.route"), launch_tier="medium"))
    model = h.only("model.route")
    assert (model["answer"], model["candidate_value"], model["match_from"]) == (
        "large",
        "large",
        "large",
    )
    assert (model["match_pick"], model["applied_value"]) == ("claude_code/large", "large")
    assert plan.model == "claude-opus-5-5"
    h.last_engine.bind_launch(plan, terminal_id="t1", model=plan.model, honored=True)
    # Covered at the decider's own size: no arrow.
    assert h.last_engine.launch_note(plan) == (
        "ran on claude-opus-5-5 (auto: large 0.90, matched long_context)"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["on", "shadow"])
async def test_model_and_effort_bound_to_match_never_ask_a_package_named_match(h, state):
    from cli_agent_orchestrator.decisions.settings import PointSettings
    from cli_agent_orchestrator.decisions.types import PointState

    impostor = Decider("match")
    h.extra = {"match": lambda: impostor}
    h.configure(features="off", route="off")
    h.settings = dataclasses.replace(
        h.settings,
        points={
            **h.settings.points,
            "model.route": PointSettings(PointState(state), "match"),
            "effort.route": PointSettings(PointState(state), "match"),
        },
    )
    plan = await h.launch(h.request(fields=("model.route", "effort.route")))
    assert impostor.requests == [] and h.decider.requests == []
    for point in ("model.route", "effort.route"):
        row = h.only(point)
        assert (row["state"], row["decider"], row["reason"]) == (
            state,
            None,
            "decider_unavailable",
        )
    assert plan is None or (plan.model, plan.effort) == ("claude-sonnet-5-5", None)


# AC20: decider-neutral.


@pytest.mark.asyncio
async def test_AC20_jev_and_jevk5_fakes_give_identical_requests_plans_and_match_columns(tmp_path):
    seen = {}
    for name in ("jev", "jevk5"):
        h = Harness(tmp_path, name, decider=Decider(name))
        features(h, long_context="yes", read_only="yes")
        plan = await h.launch()
        requests = [
            {k: v for k, v in dataclasses.asdict(r).items() if k != "request_id"}
            for r in h.decider.requests
        ]
        rows = [
            {
                k: v
                for k, v in row.items()
                if k
                not in (
                    "id",
                    "created_at",
                    "updated_at",
                    "message_hash",
                    "hash_key_id",
                    "latency_ms",
                    "decider",
                    "decider_version",
                )
            }
            for row in h.rows()
        ]
        seen[name] = (requests, plan_values(plan), rows, [r["decider"] for r in h.rows()])
    assert seen["jev"][:3] == seen["jevk5"][:3]
    assert seen["jev"][3] != seen["jevk5"][3]


# AC21: ignored features.


@pytest.mark.asyncio
async def test_AC21_ignored_feature_is_recorded_but_not_a_need(h):
    h.configure(catalog={**CATALOG, "ignore_features": ["read_only"]})
    features(h, read_only="yes")
    plan = await h.launch()
    row = h.only("task.read_only")
    assert (row["outcome"], row["answer"]) == ("applied", "yes")
    route = h.only("provider.route")
    assert (route["match_rule"], route["match_needs"]) == ("none_needed", [])
    assert plan.provider is None


def test_a_plan_with_only_hints_is_a_plan():
    assert LaunchPlan(hints=("needs_network",)).record_ids == ()
