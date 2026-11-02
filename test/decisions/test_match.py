"""#1199: the local matcher picks a CLI and tier from the options, within the bounds."""

import json
import os
import subprocess
import sys

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.decisions.catalog import MatchCatalog, ModelFacts, load_catalog
from cli_agent_orchestrator.decisions.match import (
    MATCH_RULES,
    TIEBREAKS,
    MatchInputs,
    band,
    explain_rows,
    match,
    routing_needs,
)
from cli_agent_orchestrator.services.model_tiers import TIERS

HARD = {"claude_code": "hard", "codex": "hard", "kiro_cli": "hard"}
MODEL_TIERS = {
    "claude_code": {"small": "cc-s", "medium": "cc-m", "large": "cc-l"},
    "codex": {"small": "cx-s", "medium": "cx-m", "large": "cx-l"},
    "kiro_cli": {"small": "kr-s", "medium": "kr-m", "large": "kr-l"},
}


def catalog(models, **extra):
    raw = {"version": 1, "long_context_tokens": 500000, "models": models, **extra}
    loaded = load_catalog({"decisions": {"match": raw}}, MODEL_TIERS)
    assert loaded.valid, loaded.invalid
    return loaded


def decl(context, cost, *good_for):
    return {"context_tokens": context, "cost": cost, "good_for": list(good_for)}


def inputs(
    cat,
    needs,
    *,
    options=("claude_code",),
    fallback="claude_code",
    labels=HARD,
    tiers=("medium", "large"),
    size="medium",
    model_tiers=MODEL_TIERS,
):
    return MatchInputs(
        options=options,
        labels={p: labels[p] for p in options if p in labels},
        fallback=fallback,
        tiers=tiers,
        size=size,
        needs=needs,
        model_tiers=model_tiers,
        catalog=cat,
    )


def test_the_vocabularies_are_closed():
    assert MATCH_RULES == (
        "none_needed",
        "covered",
        "no_cover",
        "no_features",
        "catalog_invalid",
        "rejected",
    )
    assert TIEBREAKS == ("only", "cost", "tier", "fallback", "order")


def test_AC5_no_need_runs_the_default_with_none_needed():
    cat = catalog({"claude_code": {"cc-l": decl(1000000, "high", "deep_reasoning")}})
    result = match(inputs(cat, ()))
    assert (result.rule, result.provider, result.tier) == ("none_needed", "claude_code", "medium")
    assert result.pick_label() is None and result.tiebreak is None


def test_needs_network_is_never_a_need_and_ignored_features_drop_out():
    cat = catalog({}, ignore_features=["read_only"])
    yes = {"long_context", "deep_reasoning", "read_only", "needs_network"}
    assert routing_needs(yes, cat) == ("long_context", "deep_reasoning")
    assert routing_needs({"needs_network"}, catalog({})) == ()
    # The fixed order holds whatever order the answers arrive in.
    assert routing_needs({"read_only", "long_context"}, catalog({})) == (
        "long_context",
        "read_only",
    )


def test_AC21_an_ignored_feature_is_not_a_need():
    cat = catalog(
        {"claude_code": {"cc-l": decl(1, "low", "read_only")}}, ignore_features=["read_only"]
    )
    needs = routing_needs({"read_only"}, cat)
    assert needs == ()
    assert match(inputs(cat, needs)).rule == "none_needed"


def test_AC6_a_cli_outside_the_options_is_never_picked():
    # Only Codex models cover long_context, but the launch's only option is Claude Code.
    cat = catalog(
        {
            "codex": {"cx-l": decl(2000000, "low", "read_only")},
            "claude_code": {"cc-m": decl(200000, "medium"), "cc-l": decl(200000, "high")},
        }
    )
    result = match(inputs(cat, ("long_context",)))
    assert (result.rule, result.provider, result.tier) == ("no_cover", "claude_code", "medium")
    assert {c.provider for c in result.candidates} == {"claude_code"}
    assert "codex" not in json.dumps(explain_rows(result))


def test_AC7_a_ceiling_of_medium_never_picks_large():
    cat = catalog({"claude_code": {"cc-m": decl(200000, "low"), "cc-l": decl(1000000, "high")}})
    result = match(
        inputs(cat, ("long_context",), tiers=band("medium", "small", "medium", raise_allowed=True))
    )
    assert (result.rule, result.tier) == ("no_cover", "medium")
    assert all(c.tier != "large" for c in result.candidates)


def test_AC7_a_size_of_large_never_picks_medium():
    # medium is cheaper and covers too, but the size answer is a floor.
    cat = catalog({"claude_code": {"cc-m": decl(900000, "low"), "cc-l": decl(1000000, "high")}})
    result = match(
        inputs(
            cat,
            ("long_context",),
            tiers=band("large", "small", "large", raise_allowed=True),
            size="large",
        )
    )
    assert (result.rule, result.provider, result.tier) == ("covered", "claude_code", "large")
    assert all(c.tier == "large" for c in result.candidates)


@pytest.mark.parametrize(
    "size,floor,ceiling,raise_allowed,expected",
    [
        ("medium", "small", "large", True, ("medium", "large")),
        ("small", "medium", "large", True, ("medium", "large")),
        ("large", "small", "large", True, ("large",)),
        ("medium", "small", "medium", True, ("medium",)),
        ("medium", "small", "large", False, ("medium",)),
        ("medium", None, None, True, ("medium",)),
        ("small", None, "large", True, ("small", "medium", "large")),
    ],
)
def test_the_band_starts_at_the_size_and_ends_at_the_ceiling(
    size, floor, ceiling, raise_allowed, expected
):
    assert band(size, floor, ceiling, raise_allowed=raise_allowed) == expected


def test_AC8_an_unreachable_declaration_is_never_a_candidate():
    # cx-ghost covers everything but no model_tiers tier maps it.
    cat = catalog({"codex": {"cx-ghost": decl(9000000, "low", "read_only", "deep_reasoning")}})
    result = match(
        inputs(cat, ("long_context",), options=("claude_code", "codex"), tiers=("medium", "large"))
    )
    assert result.rule == "no_cover"
    assert all(c.model != "cx-ghost" for c in result.candidates)
    assert {c.model for c in result.candidates} == {"cc-m", "cc-l", "cx-m", "cx-l"}


@pytest.mark.parametrize(
    "window,covered",
    [(499999, False), (500000, True), (500001, True)],
)
def test_a_window_at_long_context_tokens_covers_long_context(window, covered):
    # "at least long_context_tokens": the declared window equal to the threshold covers it.
    cat = catalog({"claude_code": {"cc-l": decl(window, "high")}})
    result = match(inputs(cat, ("long_context",)))
    assert (result.rule, result.pick_label()) == (
        ("covered", "claude_code/large") if covered else ("no_cover", None)
    )


def test_an_undeclared_model_covers_nothing_and_can_only_be_the_default():
    result = match(inputs(catalog({}), ("long_context", "read_only")))
    assert result.rule == "no_cover"
    assert all(c.facts is None and not any(c.cover) for c in result.candidates)


def test_the_most_important_need_comes_first():
    # cc-l covers long_context only; cx-m covers read_only and deep_reasoning. long_context wins.
    cat = catalog(
        {
            "claude_code": {"cc-l": decl(1000000, "high")},
            "codex": {"cx-m": decl(100000, "low", "read_only", "deep_reasoning")},
        }
    )
    needs = ("long_context", "deep_reasoning", "read_only")
    result = match(inputs(cat, needs, options=("claude_code", "codex")))
    assert (result.provider, result.tier, result.covered) == (
        "claude_code",
        "large",
        ("long_context",),
    )
    # Without long_context, the reader that covers both others wins.
    result = match(inputs(cat, ("deep_reasoning", "read_only"), options=("claude_code", "codex")))
    assert (result.provider, result.tier, result.covered) == (
        "codex",
        "medium",
        ("deep_reasoning", "read_only"),
    )


def test_a_partial_cover_is_still_covered_and_names_what_it_covers():
    cat = catalog({"claude_code": {"cc-m": decl(100000, "low", "read_only")}})
    result = match(inputs(cat, ("long_context", "read_only")))
    assert (result.rule, result.covered, result.pick_label()) == (
        "covered",
        ("read_only",),
        "claude_code/medium",
    )


TWO = ("claude_code", "codex")
THREE = ("claude_code", "codex", "kiro_cli")
TIEBREAK_CASES = [
    (
        "only",
        {"claude_code": {"cc-l": decl(1000000, "high")}},
        dict(options=TWO),
        ("claude_code", "large"),
    ),
    (
        "cost",
        {
            "claude_code": {"cc-l": decl(1000000, "high")},
            "codex": {"cx-l": decl(1000000, "medium")},
        },
        dict(options=TWO),
        ("codex", "large"),
    ),
    (
        "tier",
        {"claude_code": {"cc-m": decl(600000, "medium"), "cc-l": decl(1000000, "medium")}},
        dict(options=TWO),
        ("claude_code", "medium"),
    ),
    (
        "fallback",
        {
            "claude_code": {"cc-l": decl(1000000, "medium")},
            "codex": {"cx-l": decl(1000000, "medium")},
        },
        dict(options=TWO),
        ("claude_code", "large"),
    ),
    (
        "fallback",
        {
            "claude_code": {"cc-l": decl(1000000, "medium")},
            "codex": {"cx-l": decl(1000000, "medium")},
        },
        dict(options=TWO, fallback="codex"),
        ("codex", "large"),
    ),
    # The steps' order: cost before tier. The cheaper candidate is at the higher tier, so a tier
    # step run first would pick claude_code/medium instead.
    (
        "cost",
        {"claude_code": {"cc-m": decl(1000000, "high")}, "codex": {"cx-l": decl(1000000, "low")}},
        dict(options=TWO),
        ("codex", "large"),
    ),
    # Tier before the fallback. The creator's candidate is at the higher tier, so a fallback
    # step run first would pick claude_code/large instead.
    (
        "tier",
        {
            "claude_code": {"cc-l": decl(1000000, "medium")},
            "codex": {"cx-m": decl(1000000, "medium")},
        },
        dict(options=TWO),
        ("codex", "medium"),
    ),
    (
        "order",
        {
            "codex": {"cx-l": decl(1000000, "medium")},
            "kiro_cli": {"kr-l": decl(1000000, "medium")},
        },
        dict(options=THREE),
        ("codex", "large"),
    ),
    (
        "order",
        {
            "claude_code": {"cc-l": decl(1000000, "medium")},
            "kiro_cli": {"kr-l": decl(1000000, "medium")},
        },
        dict(options=THREE, fallback="codex"),
        ("claude_code", "large"),
    ),
]


@pytest.mark.parametrize(
    "tiebreak,models,extra,pick",
    TIEBREAK_CASES,
    ids=[f"{c[0]}-{i}" for i, c in enumerate(TIEBREAK_CASES)],
)
def test_AC10_each_tiebreak_step_decides(tiebreak, models, extra, pick):
    result = match(inputs(catalog(models), ("long_context",), **extra))
    assert (result.rule, result.tiebreak, (result.provider, result.tier)) == (
        "covered",
        tiebreak,
        pick,
    )


def test_AC10_identical_inputs_give_identical_outputs():
    cat = catalog(TIEBREAK_CASES[5][1])
    first = inputs(cat, ("long_context",), options=THREE)
    again = inputs(catalog(TIEBREAK_CASES[5][1]), ("long_context",), options=THREE)
    assert match(first) == match(again) == match(first)


@pytest.mark.parametrize(
    "fallback_label,pick_label",
    [("hard", "advisory"), ("advisory", "hard"), ("advisory", "sandboxed")],
)
def test_AC9_a_pick_mixing_advisory_is_rejected_defensively(fallback_label, pick_label):
    cat = catalog({"codex": {"cx-l": decl(1000000, "low")}})
    result = match(
        inputs(
            cat,
            ("long_context",),
            options=TWO,
            labels={"claude_code": fallback_label, "codex": pick_label},
        )
    )
    assert (result.rule, result.provider, result.tier, result.pick_label()) == (
        "rejected",
        "claude_code",
        "medium",
        None,
    )


def test_AC9_hard_with_sandboxed_is_allowed():
    cat = catalog({"codex": {"cx-l": decl(1000000, "low")}})
    result = match(
        inputs(
            cat,
            ("long_context",),
            options=TWO,
            labels={"claude_code": "hard", "codex": "sandboxed"},
        )
    )
    assert (result.rule, result.provider) == ("covered", "codex")


def test_a_pick_with_no_label_is_rejected():
    cat = catalog({"codex": {"cx-l": decl(1000000, "low")}})
    result = match(inputs(cat, ("long_context",), options=TWO, labels={"claude_code": "hard"}))
    assert result.rule == "rejected"


def test_AC11_an_invalid_catalog_matches_nothing():
    invalid = load_catalog({"decisions": {"match": {"version": 2}}})
    result = match(inputs(invalid, ("long_context",)))
    assert (result.rule, result.provider, result.tier, result.needs) == (
        "catalog_invalid",
        "claude_code",
        "medium",
        (),
    )


def test_explain_rows_mark_only_the_pick():
    cat = catalog({"claude_code": {"cc-l": decl(1000000, "high")}})
    given_inputs = inputs(cat, ("long_context",))
    rows = explain_rows(match(given_inputs))
    assert [(row["candidate"], row["pick"]) for row in rows] == [
        ("claude_code/medium", False),
        ("claude_code/large", True),
    ]
    assert rows[1]["covers"] == {"long_context": True}


# Property tests: never widens (#1174), and enforcement is preserved (AC9).
PROVIDERS = ("claude_code", "codex", "kiro_cli", "mock_cli")
MODELS = tuple(f"m{i}" for i in range(6))


@st.composite
def match_inputs(draw):
    options = tuple(p for p in PROVIDERS if draw(st.booleans())) or ("claude_code",)
    fallback = draw(st.sampled_from(options))
    labels = {p: draw(st.sampled_from(("hard", "sandboxed", "advisory"))) for p in options}
    model_tiers = {
        p: {t: draw(st.sampled_from(MODELS)) for t in TIERS if draw(st.booleans())}
        for p in PROVIDERS
    }
    declared = {
        p: {
            m: {
                "context_tokens": draw(st.integers(1, 3000000)),
                "cost": draw(st.sampled_from(("low", "medium", "high"))),
                "good_for": draw(
                    st.lists(st.sampled_from(("deep_reasoning", "read_only")), unique=True)
                ),
            }
            for m in MODELS
            if draw(st.booleans())
        }
        for p in PROVIDERS
    }
    cat = load_catalog(
        {"decisions": {"match": {"version": 1, "long_context_tokens": 500000, "models": declared}}},
        model_tiers,
    )
    ceiling = draw(st.sampled_from(TIERS))
    floor = draw(st.sampled_from(TIERS[: TIERS.index(ceiling) + 1]))
    size = draw(st.sampled_from(TIERS[TIERS.index(floor) : TIERS.index(ceiling) + 1]))
    raise_allowed = draw(st.booleans())
    needs = tuple(n for n in ("long_context", "deep_reasoning", "read_only") if draw(st.booleans()))
    return (
        MatchInputs(
            options=options,
            labels=labels,
            fallback=fallback,
            tiers=band(size, floor, ceiling, raise_allowed=raise_allowed),
            size=size,
            needs=needs,
            model_tiers=model_tiers,
            catalog=cat,
        ),
        ceiling,
    )


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(match_inputs())
def test_never_widens(drawn):
    given_inputs, ceiling = drawn
    result = match(given_inputs)
    assert result.rule in MATCH_RULES
    assert result.provider in given_inputs.options
    assert TIERS.index(result.tier) >= TIERS.index(given_inputs.size)
    assert TIERS.index(result.tier) <= max(TIERS.index(ceiling), TIERS.index(given_inputs.size))
    if result.picked:
        assert result.tier in given_inputs.tiers
        # The model always comes from model_tiers; a declaration never adds one.
        assert given_inputs.model_tiers[result.provider][result.tier]
        assert set(result.covered) <= set(given_inputs.needs) and result.covered
        assert result.tiebreak in TIEBREAKS
    else:
        assert (result.provider, result.tier) == (given_inputs.fallback, given_inputs.size)
        assert result.tiebreak is None and result.covered == ()
    for candidate in result.candidates:
        assert candidate.provider in given_inputs.options
        assert candidate.tier in given_inputs.tiers
        assert candidate.model == given_inputs.model_tiers[candidate.provider][candidate.tier]


@settings(max_examples=400, deadline=None)
@given(match_inputs())
def test_AC9_the_pick_keeps_the_fallbacks_enforcement(drawn):
    given_inputs, _ = drawn
    result = match(given_inputs)
    picked, fallback = (
        given_inputs.labels[result.provider],
        given_inputs.labels[given_inputs.fallback],
    )
    assert picked == fallback or "advisory" not in (picked, fallback)


@settings(max_examples=200, deadline=None)
@given(match_inputs())
def test_AC10_the_match_is_a_pure_function(drawn):
    given_inputs, _ = drawn
    assert match(given_inputs) == match(given_inputs)


@settings(max_examples=200, deadline=None)
@given(match_inputs())
def test_AC5_no_need_never_changes_the_default(drawn):
    given_inputs, _ = drawn
    from dataclasses import replace

    result = match(replace(given_inputs, needs=()))
    assert result.rule in ("none_needed", "catalog_invalid")
    assert (result.provider, result.tier) == (given_inputs.fallback, given_inputs.size)


def test_an_empty_catalog_is_valid_and_covers_nothing():
    assert match(inputs(MatchCatalog(), ("read_only",))).rule == "no_cover"
    assert ModelFacts(1, "low").good_for == ()


# The order tie-break must not depend on hash order: a set-based pick would vary by seed.
_PROBE = r"""
import json
from cli_agent_orchestrator.decisions.catalog import load_catalog
from cli_agent_orchestrator.decisions.match import MatchInputs, match
tiers = {p: {"large": p + "-l", "medium": p + "-m"} for p in ("claude_code", "codex", "kiro_cli", "mock_cli")}
models = {p: {p + "-l": {"context_tokens": 1000000, "cost": "medium", "good_for": ["read_only"]}}
          for p in ("mock_cli", "kiro_cli", "codex")}
cat = load_catalog({"decisions": {"match": {"version": 1, "models": models}}}, tiers)
out = []
for options, fallback in [(("claude_code", "codex", "kiro_cli", "mock_cli"), "claude_code"),
                          (("claude_code", "kiro_cli", "mock_cli"), "claude_code"),
                          (("codex", "kiro_cli", "mock_cli"), "mock_cli")]:
    r = match(MatchInputs(options, {p: "hard" for p in options}, fallback, ("medium", "large"),
                          "medium", ("long_context", "read_only"), tiers, cat))
    out.append([r.rule, r.provider, r.tier, r.tiebreak, list(r.covered)])
print(json.dumps(out))
"""
_EXPECTED = [
    ["covered", "codex", "large", "order", ["long_context", "read_only"]],
    ["covered", "kiro_cli", "large", "order", ["long_context", "read_only"]],
    ["covered", "mock_cli", "large", "fallback", ["long_context", "read_only"]],
]


def test_AC10_the_pick_is_identical_under_hash_seeds_0_to_7():
    for seed in range(8):
        env = {**os.environ, "PYTHONHASHSEED": str(seed)}
        result = subprocess.run(
            [sys.executable, "-B", "-c", _PROBE],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout) == _EXPECTED, seed
