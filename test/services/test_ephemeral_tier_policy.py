"""E2 policy contracts use the same snapshot as ephemeral creation and claims."""

import hashlib
import itertools
import json
import subprocess
import sys
from pathlib import Path
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
from unittest.mock import Mock

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.decisions.policy import DecisionInputError, PolicyConfigError
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.model_tiers import load_model_tiers


def test_A_S1_one_snapshot(create_store, monkeypatch):
    service, _, _, _, raw = create_store
    raw["model_tiers"] = {"claude_code": {"small": "model-small"}}
    reader = Mock(return_value=raw)
    monkeypatch.setattr(settings_service, "_load_or_raise", reader)
    settings = service.read_settings()
    assert settings["_model_tiers"] == load_model_tiers(raw)
    reader.assert_called_once()
    reader.reset_mock()
    name = create(create_store)["name"]
    reader.assert_called_once()
    reader.reset_mock()
    claimed = service.claim_ephemeral_agent(name, {}, CALLER)
    reader.assert_called_once()
    service.end_claim(name, claimed["claim_id"])
    reader.reset_mock()
    service.bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
    reader.assert_called_once()
    assert database.get_ephemeral_agent(name)["state"] == "launched"


def mapped(raw):
    raw["model_tiers"] = {
        p: {t: "private-" + p + "-" + t for t in ("small", "medium", "large")}
        for p in ("claude_code", "codex")
    }


def assert_no_create(env, spec, rule):
    with pytest.raises(env[0].EphemeralPolicyError) as err:
        create(env, spec)
    assert err.value.rule == rule and err.value.status_code == 400
    assert not (env[2] / "ephemeral").exists()
    with env[1]() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0
    return err.value


@pytest.mark.parametrize("key", ["max_tier", "default_tier"])
@pytest.mark.parametrize("value", ["huge", 3, True, ["small"]])
def test_A_V1_closed_sets(create_store, key, value):
    create_store[4]["ephemeral"][key] = value
    err = assert_no_create(create_store, SPEC, "policy_config_error:" + key)
    assert "ephemeral." + key in err.detail
    assert all(tier in err.detail for tier in ("small", "medium", "large"))


@pytest.mark.parametrize("default", [None, "large"])
def test_A_V2_relation(create_store, default):
    create_store[4]["ephemeral"].update(max_tier="medium", default_tier=default)
    err = assert_no_create(create_store, SPEC, "policy_config_error:default_tier")
    assert "ephemeral.default_tier" in err.detail and "ephemeral.max_tier" in err.detail


@pytest.mark.parametrize("invalid", [None, "bad id", "x" * 129])
@pytest.mark.parametrize("point", ["default", "ceiling"])
def test_A_V3_A_V4_mapping_coverage(create_store, invalid, point):
    raw = create_store[4]
    mapped(raw)
    raw["ephemeral"].update(allowed_providers=["claude_code", "codex"], default_tier="small")
    tier = "small" if point == "default" else "medium"
    if point == "ceiling":
        raw["ephemeral"]["max_tier"] = "medium"
    raw["model_tiers"]["codex"][tier] = invalid
    err = assert_no_create(create_store, SPEC, "policy_config_error:model_tiers")
    assert "model_tiers.codex." + tier in err.detail
    assert "private-" not in err.message


@pytest.mark.parametrize(
    "default,ceiling,complete",
    itertools.product(
        [None, "small", "medium", "large"], [None, "small", "medium", "large"], [False, True]
    ),
)
def test_A_V5_policy_parity(create_store, default, ceiling, complete):
    raw = create_store[4]
    mapped(raw)
    raw["ephemeral"].update(
        default_tier=default, max_tier=ceiling, allowed_providers=["claude_code", "codex"]
    )
    if not complete:
        del raw["model_tiers"]["codex"]["small"]
    settings = create_store[0].read_settings()
    bounds = create_store[0].policy_bounds(settings)
    try:
        bounds.validate(settings["_model_tiers"])
    except PolicyConfigError:
        with pytest.raises(create_store[0].EphemeralPolicyError):
            create_store[0].validate_block(settings)
    else:
        create_store[0].validate_block(settings)


def test_A_R5_A_R6_block_error_reverts_claim(create_store):
    service, _, home, _, raw = create_store
    name = create(create_store)["name"]
    raw["ephemeral"]["default_tier"] = "small"
    with pytest.raises(service.EphemeralPolicyError) as err:
        service.claim_ephemeral_agent(name, {"idempotency_key": "attempt"}, CALLER)
    assert err.value.rule == "policy_config_error:model_tiers"
    assert "operator must fix model_tiers" in err.value.detail
    assert "ephemeral.model_tiers" not in err.value.detail
    assert "re-create" not in err.value.detail
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending"
    assert all(row[k] is None for k in ("claim_id", "claim_expires_at", "idempotency_key"))
    archive = json.loads(Path(row["audit_path"]).read_text())
    assert archive["refusals"][err.value.rule]["count"] == 1
    assert (home / "ephemeral/live" / (name + ".md")).exists()


@pytest.mark.parametrize(
    "asked,default,source",
    [
        ("small", None, "spec"),
        (None, "medium", "default"),
        ("small", "medium", "spec"),
        (None, None, "none"),
    ],
)
def test_A_C1_A_C2_A_C7_A_C8_resolution(create_store, asked, default, source, caplog):
    service, _, home, _, raw = create_store
    mapped(raw)
    raw["ephemeral"]["default_tier"] = default
    spec = {**SPEC, **({"model_tier": asked} if asked else {})}
    result = create(create_store, spec)
    name = result["name"]
    tier = asked or default
    row = database.get_ephemeral_agent(name)
    profile = frontmatter.loads((home / "ephemeral/live" / (name + ".md")).read_text())
    assert row["model_tier"] == result["model_tier"] == tier
    if tier:
        assert profile["model"] == raw["model_tiers"]["claude_code"][tier]
        # The tier adds the model key and changes nothing else in the profile.
        untiered = service.render_profile(
            name, service.validate_shape(spec), "claude_code", profile["allowedTools"], CALLER
        )
        assert {k: v for k, v in profile.metadata.items() if k != "model"} == (
            frontmatter.loads(untiered.decode()).metadata
        )
        note = result["notes"][0]
        assert tier in note and ("default_tier" in note) == (asked is None)
    else:
        assert "model" not in profile
    assert "private-" not in str(result) + caplog.text
    archive = json.loads(Path(row["audit_path"]).read_text())
    launched = archive["launch_values"]["model_tier"]
    assert (launched["requested"], launched["resolved"], launched["source"]) == (
        asked,
        tier,
        source,
    )
    assert archive["spec"] == json.loads(service.canonical_spec_bytes(service.validate_shape(spec)))


def test_A_C7_declared_spec_is_preserved(create_store, monkeypatch):
    raw = create_store[4]
    mapped(raw)
    raw["ephemeral"]["default_tier"] = "small"
    tokens = iter(range(1, 20))
    monkeypatch.setattr(create_store[0].secrets, "token_hex", lambda n: f"{next(tokens):0{n*2}x}")
    explicit = create(create_store, {**SPEC, "model_tier": "small"})
    default = create(create_store)
    assert explicit["spec_sha256"] != default["spec_sha256"]
    live = create_store[2] / "ephemeral/live"
    profiles = [
        frontmatter.loads((live / (r["name"] + ".md")).read_text()) for r in (explicit, default)
    ]
    assert profiles[0]["model"] == profiles[1]["model"]


@pytest.mark.parametrize(
    "fault,rule",
    [
        ("unmapped", "tier_unmapped"),
        ("above", "tier_exceeds_policy"),
        ("block", "policy_config_error:model_tiers"),
    ],
)
def test_A_C3_A_C4_A_C5_A_C9_refusals(create_store, fault, rule, caplog):
    raw = create_store[4]
    mapped(raw)
    del raw["model_tiers"]["claude_code"]["large"]
    spec = {**SPEC, "model_tier": "large"}
    if fault != "unmapped":
        raw["ephemeral"].update(max_tier="medium", default_tier="small")
    if fault == "block":
        del raw["model_tiers"]["claude_code"]["small"]
        spec["model_tier"] = "small"
    err = assert_no_create(create_store, spec, rule)
    if fault == "above":
        assert "model_tier=large" in err.detail and "ephemeral.max_tier=medium" in err.detail
    if fault == "unmapped":
        assert "model_tiers.claude_code.large is not set or not valid" in err.detail
    assert "private-" not in err.message + caplog.text


@pytest.mark.parametrize("field", ["model_tier", "effort"])
def test_A_C6_auto_before_provider(create_store, field):
    err = assert_no_create(
        create_store,
        {**SPEC, "provider": "kiro_cli", field: "auto"},
        "auto_requires_decision_platform",
    )
    assert err.detail == field


@pytest.mark.parametrize("claimless", [False, True])
@pytest.mark.parametrize(
    "fault,expected",
    [
        ("tightened", "tier_exceeds_policy"),
        ("unmapped", "tier_unmapped"),
        ("stored_null", "tier_exceeds_policy"),
        ("provider_first", "provider_not_allowed"),
        ("broken", "policy_config_error:model_tiers"),
        ("double", "policy_config_error:model_tiers"),
    ],
)
def test_A_R1_to_A_R6_A_R9_stored_policy(create_store, claimless, fault, expected):
    service, _, home, _, raw = create_store
    mapped(raw)
    raw["ephemeral"]["default_tier"] = "large" if fault != "stored_null" else None
    name = create(create_store)["name"]
    if fault in ("tightened", "stored_null", "provider_first"):
        raw["ephemeral"].update(max_tier="medium", default_tier="small")
    if fault == "provider_first":
        raw["ephemeral"]["allowed_providers"] = ["codex"]
    if fault in ("unmapped", "double"):
        del raw["model_tiers"]["claude_code"]["large"]
    if fault == "unmapped":
        raw["ephemeral"]["default_tier"] = None
    if fault in ("broken", "double"):
        del raw["model_tiers"]["claude_code"]["small"]
        raw["ephemeral"]["default_tier"] = "small"
    rule = (
        expected
        if expected.startswith("policy_config_error:")
        else "policy_changed_since_create:" + expected
    )
    with pytest.raises(service.EphemeralPolicyError) as err:
        if claimless:
            service.bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
        else:
            service.claim_ephemeral_agent(name, {"idempotency_key": "attempt"}, CALLER)
    assert err.value.rule == rule
    row = database.get_ephemeral_agent(name)
    block = expected.startswith("policy_config_error:")
    assert row["state"] == ("pending" if block else "gc")
    if block:
        assert all(row[k] is None for k in ("claim_id", "claim_expires_at", "idempotency_key"))
        assert "operator must fix model_tiers" in err.value.detail
        assert "ephemeral.model_tiers" not in err.value.detail
        assert "re-create" not in err.value.detail
    else:
        assert row["gc_reason"] == "policy_changed"
        assert "re-create" in err.value.detail
        if fault == "stored_null":
            assert "provider default" in err.value.detail
    assert (home / "ephemeral/live" / (name + ".md")).exists() == block
    assert (home / "ephemeral/live" / (name + ".spec.json")).exists() == block
    archive = json.loads(Path(row["audit_path"]).read_text())
    assert archive["refusals"][rule]["count"] == 1
    if not block:
        assert archive["events"][-1]["event"] == "released"
    assert len(database.list_all_terminals()) == 1


@pytest.mark.parametrize("claimless", [False, True])
def test_A_R7_A_L3_remap_and_defaults_frozen(create_store, monkeypatch, claimless):
    service, _, home, _, raw = create_store
    mapped(raw)
    raw["ephemeral"]["default_tier"] = "small"
    name = create(create_store)["name"]
    live = home / "ephemeral/live" / (name + ".md")
    before = live.read_bytes()
    tokens = iter(range(1, 100))
    monkeypatch.setattr(service.secrets, "token_hex", lambda n: f"{next(tokens):0{n*2}x}")
    first = service.claim_ephemeral_agent(name, {}, CALLER)
    assert live.read_bytes() == before
    service.end_claim(name, first["claim_id"])
    del raw["model_tiers"]["claude_code"]["small"]
    with pytest.raises(service.EphemeralPolicyError, match="policy_config_error:model_tiers"):
        service.claim_ephemeral_agent(name, {}, CALLER)
    raw["model_tiers"]["claude_code"]["small"] = "remapped-small"
    raw["ephemeral"]["default_tier"] = "medium"
    if claimless:
        service.bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
        assert live.read_bytes() == before
    else:
        second = service.claim_ephemeral_agent(name, {}, CALLER)
        assert second["claim_id"] != first["claim_id"]
        assert live.read_bytes() == before.replace(b"private-claude_code-small", b"remapped-small")
        row = database.get_ephemeral_agent(name)
        assert row["model_tier"] == "small"
        assert row["profile_sha256"] == hashlib.sha256(live.read_bytes()).hexdigest()
        event = json.loads(Path(row["audit_path"]).read_text())["events"][-1]
        assert (
            event["model_tier"] == "small"
            and event["model"] == "remapped-small"
            and event["effort"] is None
        )
        assert "claim_id" not in event


def test_A_I1_pure_import():
    code = "import sys; import cli_agent_orchestrator.services.ephemeral_service; assert not any('cli_agent_orchestrator.decisions.'+x in sys.modules for x in ('engine','store'))"
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("reason", ["other", None])
def test_unknown_S1_reason_fails_closed(create_store, monkeypatch, reason):
    mapped(create_store[4])
    exc = DecisionInputError("private-detail")
    exc.reason = reason
    monkeypatch.setattr(create_store[0].PolicyBounds, "check_explicit", Mock(side_effect=exc))
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create(create_store, {**SPEC, "model_tier": "small"})
    assert err.value.rule == "unexpected_failure" and err.value.status_code == 500
    assert "private-detail" not in err.value.message
    assert not (create_store[2] / "ephemeral").exists()


@pytest.mark.parametrize("key", ["max_effort", "default_effort"])
@pytest.mark.parametrize("value", ["extreme", 2, True, ["low"]])
def test_B_V1_effort_closed_sets(create_store, key, value):
    create_store[4]["ephemeral"][key] = value
    err = assert_no_create(create_store, SPEC, "policy_config_error:" + key)
    assert "ephemeral." + key in err.detail
    assert all(effort in err.detail for effort in ("low", "medium", "high"))


@pytest.mark.parametrize("default", [None, "high"])
def test_B_V2_effort_relation(create_store, default):
    create_store[4]["ephemeral"].update(max_effort="medium", default_effort=default)
    err = assert_no_create(create_store, SPEC, "policy_config_error:default_effort")
    assert "ephemeral.default_effort" in err.detail and "ephemeral.max_effort" in err.detail


@pytest.mark.parametrize(
    "default,ceiling", itertools.product([None, "low", "medium", "high"], repeat=2)
)
@pytest.mark.parametrize("tier_default", [None, "small"])
def test_B_V3_effort_policy_parity(create_store, default, ceiling, tier_default):
    raw = create_store[4]
    mapped(raw)
    raw["ephemeral"].update(default_effort=default, max_effort=ceiling, default_tier=tier_default)
    settings = create_store[0].read_settings()
    try:
        create_store[0].policy_bounds(settings).validate(settings["_model_tiers"])
    except PolicyConfigError:
        with pytest.raises(create_store[0].EphemeralPolicyError):
            create_store[0].validate_block(settings)
    else:
        create_store[0].validate_block(settings)


def test_B_R2_broken_effort_reverts(create_store):
    service = create_store[0]
    name = create(create_store)["name"]
    create_store[4]["ephemeral"]["max_effort"] = "medium"
    with pytest.raises(service.EphemeralPolicyError) as err:
        service.claim_ephemeral_agent(name, {}, CALLER)
    assert err.value.rule == "policy_config_error:default_effort"
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending"
    assert all(row[k] is None for k in ("claim_id", "claim_expires_at", "idempotency_key"))
    assert "operator must fix ephemeral.default_effort" in err.value.detail


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
@pytest.mark.parametrize(
    "asked,default,source",
    [
        ("high", None, "spec"),
        (None, "medium", "default"),
        ("low", "medium", "spec"),
        (None, None, "none"),
    ],
)
def test_B_C1_B_C3_B_N1_effort_resolution(create_store, provider, asked, default, source, caplog):
    service, _, home, _, raw = create_store
    raw["ephemeral"].update(allowed_providers=["claude_code", "codex"], default_effort=default)
    spec = {**SPEC, "provider": provider, **({"effort": asked} if asked else {})}
    result = create(create_store, spec)
    row = database.get_ephemeral_agent(result["name"])
    effort = asked or default
    profile = frontmatter.loads((home / "ephemeral/live" / (result["name"] + ".md")).read_text())
    assert row["effort"] == result["effort"] == effort
    key = "claudeConfig" if provider == "claude_code" else "codexConfig"
    config_key = "effort" if provider == "claude_code" else "model_reasoning_effort"
    if effort:
        # Exact on purpose: the provider config carries the effort key and nothing else.
        assert profile[key] == {config_key: effort}
        # Effort adds that one config key and changes nothing else in the profile.
        plain = service.render_profile(
            result["name"],
            service.validate_shape(spec),
            provider,
            profile["allowedTools"],
            CALLER,
        )
        assert {k: v for k, v in profile.metadata.items() if k != key} == (
            frontmatter.loads(plain.decode()).metadata
        )
        note = result["notes"][1]
        assert effort in note and ("default_effort" in note) == (asked is None)
    else:
        assert "claudeConfig" not in profile and "codexConfig" not in profile
    archive = json.loads(Path(row["audit_path"]).read_text())
    launched = archive["launch_values"]["effort"]
    assert (launched["requested"], launched["resolved"], launched["source"]) == (
        asked,
        effort,
        source,
    )
    before = (home / "ephemeral/live" / (result["name"] + ".md")).read_bytes()
    service.claim_ephemeral_agent(result["name"], {}, CALLER)
    assert (home / "ephemeral/live" / (result["name"] + ".md")).read_bytes() == before
    assert "not applied yet" not in str(result) + caplog.text
    assert not hasattr(service, "_ignored_policy_notice") and not hasattr(
        service, "_warn_ignored_policy"
    )
    assert "_ignored_policy" not in service.read_settings()


def test_B_C2_effort_order_and_ceiling(create_store):
    raw = create_store[4]
    mapped(raw)
    raw["ephemeral"].update(
        default_tier="small", max_tier="medium", default_effort="low", max_effort="medium"
    )
    err = assert_no_create(
        create_store, {**SPEC, "model_tier": "large", "effort": "high"}, "tier_exceeds_policy"
    )
    err = assert_no_create(
        create_store, {**SPEC, "model_tier": "small", "effort": "high"}, "effort_exceeds_policy"
    )
    assert "effort=high" in err.detail and "ephemeral.max_effort=medium" in err.detail
    assert_no_create(
        create_store, {**SPEC, "provider": "kiro_cli", "effort": "low"}, "provider_unsupported"
    )


@pytest.mark.parametrize("claimless", [False, True])
@pytest.mark.parametrize("null", [False, True])
def test_B_R1_stored_effort_and_A5_3(create_store, claimless, null):
    service, _, home, _, raw = create_store
    name = create(create_store, SPEC if null else {**SPEC, "effort": "high"})["name"]
    raw["ephemeral"].update(max_effort="medium", default_effort="low")
    with pytest.raises(service.EphemeralPolicyError) as err:
        if claimless:
            service.bind_ephemeral_agent(name, "eeeeeeee", CALLER, "claude_code", None, None)
        else:
            service.claim_ephemeral_agent(name, {}, CALLER)
    assert err.value.rule == "policy_changed_since_create:effort_exceeds_policy"
    assert "re-create" in err.value.detail
    if null:
        assert "provider default" in err.value.detail
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert not (home / "ephemeral/live" / (name + ".md")).exists()
    assert not (home / "ephemeral/live" / (name + ".spec.json")).exists()
    assert json.loads(Path(row["audit_path"]).read_text())["events"][-1]["event"] == "released"


def test_unknown_keys_ignored(create_store, caplog):
    create_store[4]["ephemeral"]["min_tier"] = "private-setting"
    result = create(create_store)
    assert "private-setting" not in str(result) + caplog.text
    assert not caplog.records
