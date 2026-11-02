"""#1083 Q2 (provisional): the optional ephemeral.min_effort floor, following #1068's tier floor."""

import json
import logging
from pathlib import Path
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
from test.services.test_ephemeral_tier_floor import stored_refusal
from test.services.test_ephemeral_tier_policy import assert_no_create, mapped

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services.settings_service import EPHEMERAL_DEFAULTS


@pytest.fixture
def floor_store(create_store, monkeypatch):
    mapped(create_store[4])
    tokens = iter(range(1, 100))
    monkeypatch.setattr(create_store[0].secrets, "token_hex", lambda n: f"{next(tokens):0{n*2}x}")
    return create_store


def test_the_floor_is_a_known_key_off_by_default():
    assert "min_effort" in EPHEMERAL_DEFAULTS and EPHEMERAL_DEFAULTS["min_effort"] is None


@pytest.mark.parametrize("value", ["huge", 3, True, ["low"]])
def test_Q2_floor_value_is_in_the_closed_set(floor_store, value):
    floor_store[4]["ephemeral"].update(min_effort=value, default_effort="medium")
    err = assert_no_create(floor_store, SPEC, "policy_config_error:min_effort")
    assert "ephemeral.min_effort" in err.detail
    assert all(effort in err.detail for effort in ("low", "medium", "high"))


@pytest.mark.parametrize(
    "bounds,words",
    [
        ({"min_effort": "high", "default_effort": "medium", "max_effort": "medium"}, "max_effort"),
        ({"min_effort": "medium"}, "default_effort"),
        ({"min_effort": "medium", "default_effort": "low"}, "default_effort"),
        (
            {"min_effort": "high", "default_effort": "medium", "max_effort": "high"},
            "default_effort",
        ),
    ],
)
def test_Q2_floor_needs_a_default_at_or_above_it_and_no_higher_than_the_ceiling(
    floor_store, bounds, words
):
    floor_store[4]["ephemeral"].update(bounds)
    err = assert_no_create(floor_store, SPEC, "policy_config_error:min_effort")
    assert "ephemeral.min_effort" in err.detail and "ephemeral." + words in err.detail


@pytest.mark.parametrize("default", ["medium", "high"])
def test_Q2_floor_need_not_equal_default(floor_store, default):
    floor_store[4]["ephemeral"].update(
        min_effort="medium", default_effort=default, max_effort="high"
    )
    assert create(floor_store)["effort"] == default
    assert create(floor_store, {**SPEC, "effort": "medium"})["effort"] == "medium"


@pytest.mark.parametrize("ceiling", [None, "high"])
def test_Q2_below_floor_refuses_with_the_effort_rule_before_the_ceiling(floor_store, ceiling):
    floor_store[4]["ephemeral"].update(
        min_effort="medium", default_effort="medium", max_effort=ceiling
    )
    err = assert_no_create(floor_store, {**SPEC, "effort": "low"}, "effort_below_policy")
    assert "effort=low" in err.detail and "ephemeral.min_effort=medium" in err.detail
    assert "tier" not in err.detail


def test_Q2_floor_absent_keeps_E2(floor_store):
    floor_store[4]["ephemeral"].update(default_effort="medium", max_effort="high")
    assert create(floor_store, {**SPEC, "effort": "low"})["effort"] == "low"


def test_Q2_an_effort_floor_never_bounds_the_tier(floor_store):
    floor_store[4]["ephemeral"].update(
        min_effort="high", default_effort="high", default_tier="medium", max_tier="large"
    )
    result = create(floor_store, {**SPEC, "model_tier": "small"})
    assert (result["model_tier"], result["effort"]) == ("small", "high")


@pytest.mark.parametrize("claimless", [False, True])
@pytest.mark.parametrize("stored", ["low", None])
def test_Q2_floor_raised_since_create_collects(floor_store, claimless, stored):
    _, _, home, _, raw = floor_store
    name = create(floor_store, {**SPEC, "effort": stored} if stored else SPEC)["name"]
    assert database.get_ephemeral_agent(name)["effort"] == stored
    raw["ephemeral"].update(min_effort="medium", default_effort="medium")
    err = stored_refusal(floor_store, name, claimless)
    assert err.rule == "policy_changed_since_create:effort_below_policy"
    assert "re-create" in err.detail and "ephemeral.min_effort=medium" in err.detail
    assert ("provider default" in err.detail) is (stored is None)
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert not (home / "ephemeral/live" / (name + ".md")).exists()
    archive = json.loads(Path(row["audit_path"]).read_text())
    assert archive["refusals"][err.rule]["count"] == 1


@pytest.mark.parametrize("claimless", [False, True])
def test_Q2_broken_floor_returns_name_to_pending(floor_store, claimless):
    _, _, home, _, raw = floor_store
    raw["ephemeral"].update(default_effort="low")
    name = create(floor_store)["name"]
    raw["ephemeral"]["min_effort"] = "high"
    err = stored_refusal(floor_store, name, claimless)
    assert err.rule == "policy_config_error:min_effort"
    assert "operator must fix ephemeral.min_effort" in err.detail
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["claim_id"] is None
    assert (home / "ephemeral/live" / (name + ".md")).exists()


def test_Q2_floor_at_stored_effort_launches(floor_store):
    service, _, home, _, raw = floor_store
    name = create(floor_store, {**SPEC, "effort": "medium"})["name"]
    raw["ephemeral"].update(min_effort="medium", default_effort="medium")
    service.claim_ephemeral_agent(name, {}, CALLER)
    profile = frontmatter.loads((home / "ephemeral/live" / (name + ".md")).read_text())
    assert profile["claudeConfig"] == {"effort": "medium"}


def test_Q2_the_floor_key_is_known_and_a_typo_still_warns(floor_store, monkeypatch, caplog):
    service, _, _, _, raw = floor_store
    monkeypatch.setattr(service, "_warned_keys", set())
    raw["ephemeral"].update(min_effort="low", default_effort="medium", max_effort="high")
    caplog.set_level(logging.WARNING, logger=service.__name__)
    create(floor_store)
    assert not caplog.records
    raw["ephemeral"]["min_effortt"] = "private-value"
    create(floor_store)
    (record,) = caplog.records
    text = record.getMessage()
    assert "ephemeral.min_effortt" in text and "private-value" not in text
