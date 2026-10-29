"""#1083 at create, claim and the pre-bind recheck: effort auto, under #1068's rules for tiers.

Provisional throughout: effort auto needs both effort bounds (Q2), the lease covers one
on-mode wait per declared field (Q3), and the decided effort reaches the provider through the
launch plan only, never the live profile (Q1, ruling C).
"""

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from test.services.test_ephemeral_claim import NOW
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
from test.services.test_ephemeral_tier_policy import assert_no_create, mapped

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.ephemeral import EphemeralSpec

AUTO = {**SPEC, "effort": "auto"}
BOTH = {**AUTO, "model_tier": "auto"}
EFFORT_BOUNDS = {"min_effort": "low", "default_effort": "medium", "max_effort": "high"}
TIER_BOUNDS = {"min_tier": "small", "default_tier": "medium", "max_tier": "large"}


@pytest.fixture
def auto_store(create_store, monkeypatch):
    mapped(create_store[4])
    create_store[4]["ephemeral"].update(EFFORT_BOUNDS)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    tokens = iter(range(1, 100))
    monkeypatch.setattr(create_store[0].secrets, "token_hex", lambda n: f"{next(tokens):0{n*2}x}")
    return create_store


def live(env, name):
    return env[2] / "ephemeral/live" / (name + ".md")


def archive(name):
    return json.loads(Path(database.get_ephemeral_agent(name)["audit_path"]).read_text())


# An auto spec stores its fallback everywhere but the spec.


def test_auto_stores_its_fallback_and_the_spec_keeps_auto(auto_store):
    service, _, home, _, _ = auto_store
    result = create(auto_store, AUTO)
    name = result["name"]
    row = database.get_ephemeral_agent(name)
    assert result["effort"] == row["effort"] == "medium"
    assert json.loads((home / "ephemeral/live" / (name + ".spec.json")).read_text())["effort"] == (
        "auto"
    )
    assert frontmatter.loads(live(auto_store, name).read_text())["claudeConfig"] == {
        "effort": "medium"
    }
    launched = archive(name)["launch_values"]["effort"]
    assert (launched["requested"], launched["resolved"], launched["source"]) == (
        "auto",
        "medium",
        "default",
    )
    note = result["notes"][1]
    assert all(word in note for word in ("auto", "decided at launch", "medium", "default_effort"))
    assert "auto" not in json.dumps({k: v for k, v in result.items() if k != "notes"})
    # The claim renders the same fallback; a decided effort reaches the provider through the
    # launch plan only (ruling C).
    service.claim_ephemeral_agent(name, {}, CALLER)
    assert frontmatter.loads(live(auto_store, name).read_text())["claudeConfig"] == {
        "effort": "medium"
    }


def test_an_auto_effort_skips_the_explicit_value_checks(auto_store):
    # "auto" is no effort value: it never meets the ceiling or the floor at create.
    auto_store[4]["ephemeral"].update(
        min_effort="medium", max_effort="medium", default_effort="medium"
    )
    assert create(auto_store, AUTO)["effort"] == "medium"


def test_the_retired_rule_has_no_producer(auto_store):
    for spec in (AUTO, {**AUTO, "provider": "codex"}, {**AUTO, "model_tier": "small"}):
        auto_store[4]["ephemeral"]["allowed_providers"] = ["claude_code", "codex"]
        assert create(auto_store, spec)["effort"] == "medium"


# Q2: effort auto needs both effort bounds, at create and at claim.


@pytest.mark.parametrize(
    "missing", [("min_effort",), ("max_effort",), ("min_effort", "max_effort")]
)
def test_Q2_auto_needs_both_effort_bounds_at_create(auto_store, missing):
    for key in missing:
        auto_store[4]["ephemeral"][key] = None
    err = assert_no_create(auto_store, AUTO, "auto_requires_bounds")
    assert "effort=auto" in err.detail and "model_tier=auto" not in err.detail
    assert all("ephemeral." + key in err.detail for key in missing)


def test_Q2_both_fields_name_every_missing_bound(auto_store):
    auto_store[4]["ephemeral"].update(min_effort=None, default_tier="medium", max_tier="large")
    err = assert_no_create(auto_store, BOTH, "auto_requires_bounds")
    assert "model_tier=auto requires ephemeral.min_tier" in err.detail
    assert "effort=auto requires ephemeral.min_effort" in err.detail
    assert "max_tier" not in err.detail and "max_effort" not in err.detail


def test_Q2_the_bounds_refusal_precedes_the_provider_checks(auto_store):
    auto_store[4]["ephemeral"]["max_effort"] = None
    assert_no_create(auto_store, {**AUTO, "provider": "kiro_cli"}, "auto_requires_bounds")


@pytest.mark.parametrize("claimless", [False, True])
@pytest.mark.parametrize(
    "spec,change,rule",
    [
        (AUTO, {"min_effort": None}, "policy_config_error:min_effort"),
        (AUTO, {"max_effort": None}, "policy_config_error:max_effort"),
        (AUTO, {"min_effort": None, "max_effort": None}, "policy_config_error:min_effort"),
        # The first missing key in the order min_tier, max_tier, min_effort, max_effort.
        (BOTH, {"min_effort": None, "max_tier": None}, "policy_config_error:max_tier"),
        (BOTH, {"max_effort": None}, "policy_config_error:max_effort"),
        (AUTO, {"claim_lease_seconds": 30}, "policy_config_error:claim_lease_seconds"),
        (BOTH, {"claim_lease_seconds": 31}, "policy_config_error:claim_lease_seconds"),
    ],
)
def test_Q2_Q3_a_bound_or_lease_broken_after_create_returns_the_name(
    auto_store, claimless, spec, change, rule
):
    service, _, _, _, raw = auto_store
    raw["ephemeral"].update(TIER_BOUNDS)
    name = create(auto_store, spec)["name"]
    raw["ephemeral"].update(change)
    with pytest.raises(service.EphemeralPolicyError) as err:
        if claimless:
            service.bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
        else:
            service.claim_ephemeral_agent(name, {"idempotency_key": "attempt"}, CALLER)
    assert err.value.rule == rule
    assert "operator must fix ephemeral." + rule.partition(":")[2] in err.value.detail
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["claim_id"] is None
    assert live(auto_store, name).exists()


def test_Q2_the_same_change_leaves_an_explicit_effort_alone(auto_store):
    service, _, _, _, raw = auto_store
    name = create(auto_store, {**SPEC, "effort": "medium"})["name"]
    raw["ephemeral"].update(min_effort=None, claim_lease_seconds=5)
    assert service.claim_ephemeral_agent(name, {}, CALLER)["replayed"] is False


# Q3: the lease covers one on-mode wait per field declared auto.


@pytest.mark.parametrize(
    "spec,lease,timeout,valid",
    [
        (AUTO, 31, 1000, True),
        (BOTH, 31, 1000, False),
        (BOTH, 32, 1000, True),
        (AUTO, 35, 5000, True),
        (BOTH, 39, 5000, False),
        (BOTH, 40, 5000, True),
    ],
)
def test_Q3_the_lease_floor_counts_each_declared_field(auto_store, spec, lease, timeout, valid):
    raw = auto_store[4]
    raw["ephemeral"].update(TIER_BOUNDS, claim_lease_seconds=lease)
    raw["decisions"] = {"on_timeout_ms": timeout}
    if valid:
        assert create(auto_store, spec)["effort"] == "medium"
    else:
        err = assert_no_create(auto_store, spec, "policy_config_error:claim_lease_seconds")
        assert "2 * decisions.on_timeout_ms" in err.detail
        assert "model_tier=auto" in err.detail and "effort=auto" in err.detail


def test_Q3_the_default_lease_covers_both_fields_at_the_maximum_timeout(auto_store):
    raw = auto_store[4]
    raw["ephemeral"].update(TIER_BOUNDS)
    raw["decisions"] = {"on_timeout_ms": 5000}
    assert raw["ephemeral"].get("claim_lease_seconds") is None  # the 60 s default
    assert create(auto_store, BOTH)["effort"] == "medium"


# The decision inputs: only declared fields, model first, each with its stored fallback.


def claimed(env, spec):
    name = create(env, spec)["name"]
    return name, env[0].claim_ephemeral_agent(name, {}, CALLER)["claim_id"]


@pytest.mark.parametrize(
    "spec,fields",
    [
        (AUTO, ["effort.route"]),
        (BOTH, ["model.route", "effort.route"]),
        ({**SPEC, "model_tier": "auto"}, ["model.route"]),
    ],
)
def test_decision_inputs_ask_only_declared_fields_model_first(auto_store, spec, fields):
    service, _, _, _, raw = auto_store
    raw["ephemeral"].update(TIER_BOUNDS)
    name, token = claimed(auto_store, spec)
    # M4: a default changed after create never re-resolves a declared field's fallback.
    raw["ephemeral"].update(default_effort="high", default_tier="large")
    inputs = service.decision_inputs(name, token, CALLER, "claude_code")
    assert list(inputs.field_states) == fields
    assert set(inputs.field_states.values()) == {"auto"}
    policy = inputs.policy
    assert (policy.min_effort, policy.max_effort) == ("low", "high")
    assert policy.default_effort == ("medium" if "effort.route" in fields else "high")
    assert policy.default_tier == ("medium" if "model.route" in fields else "large")


@pytest.mark.parametrize("spec", [SPEC, {**SPEC, "effort": "low"}, {**SPEC, "model_tier": "small"}])
def test_an_explicit_or_omitted_effort_never_opts_in(auto_store, spec):
    service = auto_store[0]
    name, token = claimed(auto_store, spec)
    assert service.decision_inputs(name, token, CALLER, "claude_code") is None


def test_a_forged_auto_effort_never_opts_in(auto_store):
    # The forged spec parses as a valid auto spec, so only the hash check refuses it.
    service, _, home, _, _ = auto_store
    name, token = claimed(auto_store, SPEC)
    path = home / "ephemeral/live" / (name + ".spec.json")
    forged = json.dumps({**json.loads(path.read_bytes()), "effort": "auto"}).encode()
    assert EphemeralSpec.model_validate(json.loads(forged)).effort == "auto"
    path.write_bytes(forged)
    assert service.decision_inputs(name, token, CALLER, "claude_code") is None


# The pre-bind recheck (#1068 C, #1083): the decided model and effort are returned to the
# caller for the launch plan only; recheck_decided itself never writes anything.


def test_recheck_returns_the_decided_effort_with_the_current_tables_model(auto_store):
    service, _, _, _, raw = auto_store
    raw["ephemeral"]["default_tier"] = "small"
    name, token = claimed(auto_store, AUTO)
    payload_at_claim = live(auto_store, name).read_bytes()
    raw["model_tiers"]["claude_code"]["small"] = "private-next-small"
    decided = service.recheck_decided(name, token, CALLER, None, "high")
    assert decided.model == "private-next-small"
    assert decided.effort == "high"
    # Nothing is written: the row keeps the stored effort and the live profile, rendered at
    # claim time with the old table, is untouched (#1068 C, #1083).
    row = database.get_ephemeral_agent(name)
    assert row["effort"] == "medium" and row["model_tier"] == "small"
    payload = live(auto_store, name).read_bytes()
    assert payload == payload_at_claim
    assert hashlib.sha256(payload).hexdigest() == row["profile_sha256"]
    profile = frontmatter.loads(payload.decode())
    assert profile["claudeConfig"] == {"effort": "medium"}
    assert profile["model"] == "private-claude_code-small"
    assert [e["event"] for e in archive(name)["events"]] == ["created", "claimed", "finalized"]


@pytest.mark.parametrize(
    "change,effort,rule",
    [
        ({"max_effort": "medium"}, "high", "effort_exceeds_policy"),
        ({"min_effort": "medium"}, "low", "effort_below_policy"),
        ({"max_effort": "low", "default_effort": "low"}, None, "effort_exceeds_policy"),
    ],
)
def test_recheck_collects_an_effort_now_outside_the_bounds(auto_store, change, effort, rule):
    service, _, _, _, raw = auto_store
    name, token = claimed(auto_store, AUTO)
    raw["ephemeral"].update(change)
    with pytest.raises(service.EphemeralPolicyError) as err:
        service.recheck_decided(name, token, CALLER, None, effort)
    assert err.value.rule == "policy_changed_since_create:" + rule
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert not live(auto_store, name).exists()


def test_recheck_launches_an_effort_still_within_the_bounds(auto_store):
    service, _, _, _, raw = auto_store
    name, token = claimed(auto_store, AUTO)
    raw["ephemeral"].update(min_effort="medium", default_effort="high")
    decided = service.recheck_decided(name, token, CALLER, None, "high")
    assert decided.effort == "high"
    assert database.get_ephemeral_agent(name)["state"] == "claimed"


def test_recheck_answers_only_for_the_live_claim(auto_store, monkeypatch):
    service = auto_store[0]
    name, token = claimed(auto_store, AUTO)
    assert service.recheck_decided(name, "f" * 32, CALLER, None, "high") is None
    monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=61))
    assert service.recheck_decided(name, token, CALLER, None, "high") is None


# Under C, recheck_decided writes nothing, so there is no second finalize to fence or to
# collect a missing spec; finalize's own claim-expired and spec_unavailable paths are already
# covered at the claim (test_ephemeral_claim.py), including E9's tightened window (#1068).
