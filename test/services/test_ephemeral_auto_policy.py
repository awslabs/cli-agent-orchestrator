"""#1068 at create and claim: model_tier auto needs both bounds and a long enough lease."""

import json
from dataclasses import replace
from pathlib import Path
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401
from test.services.test_ephemeral_tier_policy import assert_no_create, mapped

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.ephemeral import EphemeralSpec
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps

AUTO = {**SPEC, "model_tier": "auto"}
BOUNDS = {"min_tier": "small", "default_tier": "medium", "max_tier": "large"}


@pytest.fixture
def auto_store(create_store, monkeypatch):
    mapped(create_store[4])
    create_store[4]["ephemeral"].update(BOUNDS)
    tokens = iter(range(1, 100))
    monkeypatch.setattr(create_store[0].secrets, "token_hex", lambda n: f"{next(tokens):0{n*2}x}")
    return create_store


@pytest.mark.parametrize("missing", [("min_tier",), ("max_tier",), ("min_tier", "max_tier")])
def test_MF4_auto_needs_both_bounds_at_create(auto_store, missing):
    for key in missing:
        auto_store[4]["ephemeral"][key] = None
    err = assert_no_create(auto_store, AUTO, "auto_requires_bounds")
    assert all("ephemeral." + key in err.detail for key in missing)


def test_MF4_bounds_refusal_precedes_the_provider_checks(auto_store):
    auto_store[4]["ephemeral"]["max_tier"] = None
    assert_no_create(auto_store, {**AUTO, "provider": "kiro_cli"}, "auto_requires_bounds")


# #1074: an auto spec runs the same provider checks and reports the provider's label.


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_an_auto_create_reports_the_providers_enforcement_label(auto_store, provider):
    auto_store[4]["ephemeral"]["allowed_providers"] = ["claude_code", "codex"]
    result = create(auto_store, {**AUTO, "provider": provider})
    assert result["provider"] == provider and result["model_tier"] == "medium"
    assert result["enforcement"] == caps.capability_for(provider).enforcement


def test_an_auto_create_refuses_an_atom_the_provider_cannot_run(auto_store, monkeypatch):
    row = caps.CAPABILITIES["claude_code"]
    monkeypatch.setitem(
        caps.CAPABILITIES, "claude_code", replace(row, tool_atoms=row.tool_atoms - {"fs_read"})
    )
    assert_no_create(auto_store, AUTO, "tool_unsupported_on_provider:fs_read")


def test_D1_effort_auto_still_refuses(auto_store):
    err = assert_no_create(
        auto_store, {**SPEC, "effort": "auto"}, "auto_requires_decision_platform"
    )
    assert err.detail == "effort"


def test_F1_auto_stores_its_fallback_and_the_spec_keeps_auto(auto_store):
    service, _, home, _, raw = auto_store
    result = create(auto_store, AUTO)
    omitted = create(auto_store)
    name = result["name"]
    row = database.get_ephemeral_agent(name)
    assert result["model_tier"] == row["model_tier"] == "medium"
    assert result["spec_sha256"] != omitted["spec_sha256"]
    note = result["notes"][0]
    assert all(word in note for word in ("auto", "medium", "ephemeral.default_tier"))
    live = home / "ephemeral/live"
    assert json.loads((live / (name + ".spec.json")).read_text())["model_tier"] == "auto"
    profile = frontmatter.loads((live / (name + ".md")).read_text())
    assert profile["model"] == raw["model_tiers"]["claude_code"]["medium"]
    archive = json.loads(Path(row["audit_path"]).read_text())
    launched = archive["launch_values"]["model_tier"]
    assert (launched["requested"], launched["resolved"], launched["source"]) == (
        "auto",
        "medium",
        "default",
    )
    # The claim renders the same fallback; the decision applies through the launch plan.
    service.claim_ephemeral_agent(name, {}, CALLER)
    assert frontmatter.loads((live / (name + ".md")).read_text())["model"] == profile["model"]


@pytest.mark.parametrize(
    "lease,timeout,valid",
    [(30, 1000, False), (31, 1000, True), (34, 5000, False), (35, 5000, True)],
)
def test_A15_lease_floor_at_create(auto_store, lease, timeout, valid):
    raw = auto_store[4]
    raw["ephemeral"]["claim_lease_seconds"] = lease
    raw["decisions"] = {"on_timeout_ms": timeout}
    if valid:
        assert create(auto_store, AUTO)["model_tier"] == "medium"
    else:
        err = assert_no_create(auto_store, AUTO, "policy_config_error:claim_lease_seconds")
        assert "decisions.on_timeout_ms" in err.detail


def test_A15_lease_floor_uses_the_effective_timeout(auto_store, monkeypatch):
    raw = auto_store[4]
    raw["ephemeral"]["claim_lease_seconds"] = 34
    raw["decisions"] = {"on_timeout_ms": 50}
    monkeypatch.setenv("CAO_DECISION_ON_TIMEOUT_MS", "5000")
    assert_no_create(auto_store, AUTO, "policy_config_error:claim_lease_seconds")


def test_A15_lease_floor_applies_only_to_auto(auto_store):
    auto_store[4]["ephemeral"]["claim_lease_seconds"] = 5
    assert create(auto_store)["model_tier"] == "medium"
    assert create(auto_store, {**SPEC, "model_tier": "small"})["model_tier"] == "small"


@pytest.mark.parametrize("claimless", [False, True])
@pytest.mark.parametrize(
    "change,rule",
    [
        ({"min_tier": None}, "policy_config_error:min_tier"),
        ({"max_tier": None}, "policy_config_error:max_tier"),
        ({"claim_lease_seconds": 30}, "policy_config_error:claim_lease_seconds"),
    ],
)
def test_MF4_A15_a_rule_broken_after_create_returns_the_name(auto_store, claimless, change, rule):
    service, _, home, _, raw = auto_store
    name = create(auto_store, AUTO)["name"]
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
    assert (home / "ephemeral/live" / (name + ".md")).exists()


def test_MF4_the_same_change_leaves_an_explicit_spec_alone(auto_store):
    service, _, _, _, raw = auto_store
    name = create(auto_store, {**SPEC, "model_tier": "medium"})["name"]
    raw["ephemeral"].update(min_tier=None, claim_lease_seconds=5)
    assert service.claim_ephemeral_agent(name, {}, CALLER)["replayed"] is False


def test_F1_an_unreadable_spec_never_opts_in(auto_store):
    service, _, home, _, _ = auto_store
    name = create(auto_store, AUTO)["name"]
    row = database.get_ephemeral_agent(name)
    assert service._stored_spec(row).model_tier == "auto"
    (home / "ephemeral/live" / (name + ".spec.json")).write_text('{"model_tier": "auto"}')
    assert service._stored_spec(row) is None


def test_F1_a_valid_spec_replaced_after_create_never_opts_in(auto_store):
    # The forged spec parses as a valid auto spec, so only the hash check refuses it.
    service, _, home, _, _ = auto_store
    name = create(auto_store)["name"]
    path = home / "ephemeral/live" / (name + ".spec.json")
    forged = json.dumps({**json.loads(path.read_bytes()), "model_tier": "auto"}).encode()
    assert EphemeralSpec.model_validate(json.loads(forged)).model_tier == "auto"
    token = service.claim_ephemeral_agent(name, {}, CALLER)["claim_id"]
    path.write_bytes(forged)
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "claimed" and row["claim_id"] == token
    assert service._stored_spec(row) is None
    assert service.decision_inputs(name, token, CALLER, "claude_code") is None


def test_the_spec_file_is_never_rewritten_by_a_launch_a_refusal_or_a_collection(
    auto_store, monkeypatch
):
    """decision_inputs's lenient _stored_spec read never feeds a write."""
    service, _, home, _, raw = auto_store

    def spec_path(name):
        return home / "ephemeral/live" / (name + ".spec.json")

    # 1. A successful auto launch: the spec is read, never rewritten.
    name = create(auto_store, AUTO)["name"]
    original = spec_path(name).read_bytes()
    token = service.claim_ephemeral_agent(name, {}, CALLER)["claim_id"]
    assert service.decision_inputs(name, token, CALLER, "claude_code") is not None
    assert spec_path(name).read_bytes() == original

    # 2. A refusal that reverts the claim (a broken block), not a collection.
    name2 = create(auto_store, AUTO)["name"]
    original2 = spec_path(name2).read_bytes()
    token2 = service.claim_ephemeral_agent(name2, {}, CALLER)["claim_id"]
    raw["ephemeral"]["min_tier"] = None
    with pytest.raises(service.EphemeralPolicyError) as err2:
        service.decision_inputs(name2, token2, CALLER, "claude_code")
    assert err2.value.rule == "policy_config_error:min_tier"
    assert spec_path(name2).read_bytes() == original2
    raw["ephemeral"]["min_tier"] = BOUNDS["min_tier"]

    # 3. A tightened policy that collects the name: the file is unlinked, never rewritten.
    name3 = create(auto_store, AUTO)["name"]
    assert spec_path(name3).read_bytes() == original  # still the same bytes, right up to collect
    token3 = service.claim_ephemeral_agent(name3, {}, CALLER)["claim_id"]
    raw["ephemeral"].update(max_tier="small", default_tier="small")
    unlinked_bytes = []
    real_unlink = service.os.unlink

    def spying_unlink(path, *args, **kwargs):
        # Read the live bytes at the exact moment of the unlink, via the real path, before
        # calling through: a rewrite staged just ahead of the unlink would show up here.
        if path == name3 + ".spec.json":
            unlinked_bytes.append(spec_path(name3).read_bytes())
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(service.os, "unlink", spying_unlink)
    with pytest.raises(service.EphemeralPolicyError) as err3:
        service.decision_inputs(name3, token3, CALLER, "claude_code")
    assert err3.value.rule == "policy_changed_since_create:tier_exceeds_policy"
    assert unlinked_bytes == [original]
    assert not spec_path(name3).exists()
