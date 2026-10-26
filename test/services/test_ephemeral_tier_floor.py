"""The optional ephemeral.min_tier floor and the unknown-key warning (#801 MF1-MF3, MF6)."""

import json
import logging
from pathlib import Path
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
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


@pytest.mark.parametrize("value", ["huge", 3, True, ["small"]])
def test_MF1_floor_value_is_in_the_closed_set(floor_store, value):
    floor_store[4]["ephemeral"].update(min_tier=value, default_tier="medium")
    err = assert_no_create(floor_store, SPEC, "policy_config_error:min_tier")
    assert "ephemeral.min_tier" in err.detail
    assert all(tier in err.detail for tier in ("small", "medium", "large"))


@pytest.mark.parametrize(
    "bounds,words",
    [
        ({"min_tier": "large", "default_tier": "medium", "max_tier": "medium"}, "max_tier"),
        ({"min_tier": "medium"}, "default_tier"),
        ({"min_tier": "medium", "default_tier": "small"}, "default_tier"),
        ({"min_tier": "large", "default_tier": "medium", "max_tier": "large"}, "default_tier"),
    ],
)
def test_MF1_rules_9_and_10(floor_store, bounds, words):
    floor_store[4]["ephemeral"].update(bounds)
    err = assert_no_create(floor_store, SPEC, "policy_config_error:min_tier")
    assert "ephemeral.min_tier" in err.detail and "ephemeral." + words in err.detail


@pytest.mark.parametrize("default", ["medium", "large"])
def test_MF1_floor_need_not_equal_default(floor_store, default):
    floor_store[4]["ephemeral"].update(min_tier="medium", default_tier=default, max_tier="large")
    result = create(floor_store)
    assert result["model_tier"] == default
    explicit = create(floor_store, {**SPEC, "model_tier": "medium"})
    assert explicit["model_tier"] == "medium"


@pytest.mark.parametrize("ceiling", [None, "large"])
def test_MF2_below_floor_refuses_before_ceiling_and_mapping(floor_store, ceiling):
    raw = floor_store[4]
    raw["ephemeral"].update(min_tier="medium", default_tier="medium", max_tier=ceiling)
    if ceiling is None:
        # Without a ceiling, an unmapped small would otherwise be tier_unmapped.
        del raw["model_tiers"]["claude_code"]["small"]
    err = assert_no_create(floor_store, {**SPEC, "model_tier": "small"}, "tier_below_policy")
    assert "model_tier=small" in err.detail and "ephemeral.min_tier=medium" in err.detail
    assert "private-" not in err.message


@pytest.mark.parametrize("missing", ["small", "medium", "large"])
def test_MF5_mappings_run_from_the_floor(floor_store, missing):
    raw = floor_store[4]
    raw["ephemeral"].update(min_tier="medium", default_tier="medium", max_tier="large")
    del raw["model_tiers"]["claude_code"][missing]
    if missing == "small":
        assert create(floor_store)["model_tier"] == "medium"
    else:
        err = assert_no_create(floor_store, SPEC, "policy_config_error:model_tiers")
        assert "model_tiers.claude_code." + missing in err.detail


def test_MF2_floor_absent_keeps_E2(floor_store):
    floor_store[4]["ephemeral"].update(default_tier="medium", max_tier="large")
    assert create(floor_store, {**SPEC, "model_tier": "small"})["model_tier"] == "small"


def stored_refusal(env, name, claimless):
    service = env[0]
    with pytest.raises(service.EphemeralPolicyError) as err:
        if claimless:
            service.bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
        else:
            service.claim_ephemeral_agent(name, {"idempotency_key": "attempt"}, CALLER)
    return err.value


@pytest.mark.parametrize("claimless", [False, True])
@pytest.mark.parametrize("stored", ["small", None])
def test_MF2_MF3_floor_raised_since_create_collects(floor_store, claimless, stored):
    _, _, home, _, raw = floor_store
    name = create(floor_store, {**SPEC, "model_tier": stored} if stored else SPEC)["name"]
    assert database.get_ephemeral_agent(name)["model_tier"] == stored
    raw["ephemeral"].update(min_tier="medium", default_tier="medium")
    err = stored_refusal(floor_store, name, claimless)
    assert err.rule == "policy_changed_since_create:tier_below_policy"
    assert "re-create" in err.detail and "ephemeral.min_tier=medium" in err.detail
    assert ("provider default" in err.detail) is (stored is None)
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert not (home / "ephemeral/live" / (name + ".md")).exists()
    archive = json.loads(Path(row["audit_path"]).read_text())
    assert archive["refusals"][err.rule]["count"] == 1


@pytest.mark.parametrize("claimless", [False, True])
def test_MF1_broken_floor_returns_name_to_pending(floor_store, claimless):
    _, _, home, _, raw = floor_store
    raw["ephemeral"].update(default_tier="small")
    name = create(floor_store)["name"]
    raw["ephemeral"]["min_tier"] = "large"
    err = stored_refusal(floor_store, name, claimless)
    assert err.rule == "policy_config_error:min_tier"
    assert "operator must fix ephemeral.min_tier" in err.detail
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["claim_id"] is None
    assert (home / "ephemeral/live" / (name + ".md")).exists()


def test_MF1_floor_at_stored_tier_launches(floor_store):
    service, _, home, _, raw = floor_store
    name = create(floor_store, {**SPEC, "model_tier": "medium"})["name"]
    raw["ephemeral"].update(min_tier="medium", default_tier="medium")
    service.claim_ephemeral_agent(name, {}, CALLER)
    profile = frontmatter.loads((home / "ephemeral/live" / (name + ".md")).read_text())
    assert profile["model"] == "private-claude_code-medium"


def test_MF6_unknown_keys_warn_once_by_name(floor_store, monkeypatch, caplog):
    service, _, _, _, raw = floor_store
    monkeypatch.setattr(service, "_warned_keys", set())
    raw["ephemeral"].update(min_tierr="private-value", sweep_interval=10)
    raw["ephemeral"]["child_may_delegate"] = False
    raw["ephemeral"]["bad\nkey"] = "private-other"
    caplog.set_level(logging.WARNING, logger=service.__name__)
    create(floor_store)
    (record,) = caplog.records
    text = record.getMessage()
    assert "ephemeral.min_tierr" in text and "ephemeral.sweep_interval" in text
    assert "1 more" in text and "bad" not in text
    assert "private-" not in text and "child_may_delegate" not in text
    caplog.clear()
    create(floor_store)
    assert not caplog.records
    raw["ephemeral"]["workflow_tool_ceiling"] = ["fs_read"]
    create(floor_store)
    (record,) = caplog.records
    assert record.getMessage().endswith("ephemeral.workflow_tool_ceiling")


def test_MF6_known_block_is_silent(floor_store, monkeypatch, caplog):
    service, _, _, _, raw = floor_store
    monkeypatch.setattr(service, "_warned_keys", set())
    raw["ephemeral"].update(
        min_tier="small", default_tier="medium", max_tier="large", child_may_delegate=True
    )
    # Every defaulted key is known, including the sweep, cap and retention keys.
    raw["ephemeral"].update(
        {key: value for key, value in EPHEMERAL_DEFAULTS.items() if key not in raw["ephemeral"]}
    )
    caplog.set_level(logging.WARNING, logger=service.__name__)
    create(floor_store)
    assert not caplog.records
