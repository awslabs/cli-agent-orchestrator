"""One provider helper at create, claim and policy validation (#1074 phase 3).

``tool_unsupported_on_provider:<atom>`` and the ``enforcement`` label are
provisional (#1074 open questions 4 and 6). No current provider refuses an atom,
so the refusal tests substitute a narrowed capability row.
"""

import json
from dataclasses import replace
from datetime import datetime, timezone
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401

import frontmatter
import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS, EphemeralSpec
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.utils import agent_profiles
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps

NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)
ALL_ATOMS = {**SPEC, "tools": list(TOOL_ATOMS)}


def grant_creator(env, tools, provider="claude_code"):
    with env[1]() as db:
        row = db.get(database.TerminalModel, CALLER)
        row.allowed_tools = json.dumps(tools)
        row.provider = provider
        db.commit()


def narrow(monkeypatch, provider, atoms):
    row = replace(caps.CAPABILITIES[provider], tool_atoms=frozenset(atoms))
    monkeypatch.setitem(caps.CAPABILITIES, provider, row)


def refuse(monkeypatch, provider):
    monkeypatch.setitem(caps.CAPABILITIES, provider, caps.capability_for("kiro_cli"))


def refusal(env, spec):
    with pytest.raises(env[0].EphemeralPolicyError) as err:
        create(env, spec)
    return err.value


# Rule 8 before this change, as the reference for unchanged behaviour.
def _old_rule_8(providers):
    return (
        isinstance(providers, list)
        and bool(providers)
        and all(p in ("claude_code", "codex") for p in providers)
    )


RULE_8_INPUTS = [[p.value] for p in ProviderType] + [
    ["claude_code", "codex"],
    ["codex", "kiro_cli"],
    [],
    None,
    "claude_code",
    ["Claude_Code"],
    [["claude_code"]],
    [{"provider": "codex"}],
    [None],
    [1],
]


@pytest.mark.parametrize("providers", RULE_8_INPUTS)
def test_rule_8_accepts_exactly_what_it_did(create_store, providers):
    service, _, home, _, config = create_store
    config["ephemeral"]["allowed_providers"] = providers
    settings = service.read_settings()
    if _old_rule_8(providers):
        service.validate_block(settings)
        return
    with pytest.raises(service.EphemeralPolicyError) as err:
        service.validate_block(settings)
    assert err.value.rule == "policy_config_error:allowed_providers"
    assert err.value.detail == "ephemeral.allowed_providers must contain supported providers"
    # Through create too: an unhashable entry is a policy refusal, not a 500.
    with pytest.raises(service.EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.rule == "policy_config_error:allowed_providers"
    assert err.value.status_code == 400
    assert not (home / "ephemeral").exists()


@pytest.mark.parametrize("provider", [p.value for p in ProviderType])
def test_every_provider_rule_8_accepts_has_a_supporting_capability_row(create_store, provider):
    service, _, _, _, config = create_store
    config["ephemeral"]["allowed_providers"] = [provider]
    try:
        service.validate_block(service.read_settings())
    except service.EphemeralPolicyError:
        assert provider not in caps.ephemeral_providers()
        return
    assert provider in caps.CAPABILITIES
    assert caps.CAPABILITIES[provider].supports_ephemeral


def test_rule_8_follows_the_capability_table(create_store, monkeypatch):
    service, _, _, _, config = create_store
    config["ephemeral"]["allowed_providers"] = ["codex"]
    refuse(monkeypatch, "codex")
    with pytest.raises(service.EphemeralPolicyError, match="allowed_providers"):
        service.validate_block(service.read_settings())


def test_provider_unsupported_lists_the_table_providers(create_store, monkeypatch):
    env = create_store
    refuse(monkeypatch, "codex")
    error = refusal(env, {**SPEC, "provider": "codex"})
    assert error.rule == "provider_unsupported" and error.status_code == 400
    assert error.detail == "codex; supported providers: claude_code"


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_create_reports_each_providers_enforcement_label(create_store, provider):
    env = create_store
    env[4]["ephemeral"]["allowed_providers"] = ["claude_code", "codex"]
    result = create(env, {**SPEC, "provider": provider})
    assert result["provider"] == provider
    assert result["enforcement"] == caps.capability_for(provider).enforcement
    assert result["enforcement"] == {"claude_code": caps.HARD, "codex": caps.ADVISORY}[provider]


def test_the_label_follows_the_resolved_provider_not_the_spec_field(create_store):
    env = create_store
    env[4]["ephemeral"]["allowed_providers"] = ["codex"]
    grant_creator(env, ["@cao-mcp-server", "fs_read"], provider="codex")
    result = create(env)
    assert result["provider"] == "codex" and result["enforcement"] == caps.ADVISORY


def test_unsupported_atom_refuses_at_create_with_its_rule(create_store, monkeypatch):
    env = create_store
    env[4]["ephemeral"]["allowed_providers"] = ["codex"]
    grant_creator(env, ["*"])
    narrow(monkeypatch, "codex", set(TOOL_ATOMS) - {"web_fetch"})
    suffixes = iter(range(1, 100))
    monkeypatch.setattr(env[0].secrets, "token_hex", lambda n: f"{next(suffixes):0{n * 2}x}")
    error = refusal(env, {**SPEC, "provider": "codex", "tools": ["fs_read", "web_fetch"]})
    assert error.rule == "tool_unsupported_on_provider:web_fetch" and error.status_code == 400
    assert "codex" in error.detail and "web_fetch" in error.detail
    assert not (env[2] / "ephemeral").exists()
    with env[1]() as db:
        assert db.query(database.EphemeralAgentModel).count() == 0
    # The same spec without the atom is accepted, and the other provider still runs it.
    assert create(env, {**SPEC, "provider": "codex", "tools": ["fs_read"]})["provider"] == "codex"
    env[4]["ephemeral"]["allowed_providers"] = ["claude_code", "codex"]
    assert create(env, {**SPEC, "tools": ["fs_read", "web_fetch"]})["provider"] == "claude_code"


def test_the_rule_names_the_first_atom_and_the_detail_names_all(create_store, monkeypatch):
    env = create_store
    grant_creator(env, ["*"])
    narrow(monkeypatch, "claude_code", {"fs_read", "fs_list"})
    error = refusal(env, {**SPEC, "tools": ["web_fetch", "execute_bash", "fs_write"]})
    assert error.rule == "tool_unsupported_on_provider:fs_write"
    for atom in ("fs_write", "execute_bash", "web_fetch"):
        assert atom in error.detail


def test_default_tools_are_checked_too(create_store, monkeypatch):
    env = create_store
    narrow(monkeypatch, "claude_code", {"fs_read"})
    assert refusal(env, {**SPEC, "tools": None}).rule == "tool_unsupported_on_provider:fs_list"


@pytest.mark.parametrize(
    "spec_patch,config_patch,creator_tools,rule",
    [
        # A provider the operator did not allow is reported before its atoms.
        ({"provider": "codex"}, {}, ["*"], "provider_not_allowed"),
        # Provider limits precede tier, effort and creator tool-ceiling checks (provisional order).
        (
            {"model_tier": "large"},
            {"default_tier": "small", "max_tier": "small"},
            ["*"],
            "tool_unsupported_on_provider:web_fetch",
        ),
        (
            {"effort": "high"},
            {"default_effort": "low", "max_effort": "low"},
            ["*"],
            "tool_unsupported_on_provider:web_fetch",
        ),
        ({}, {}, ["@cao-mcp-server"], "tool_unsupported_on_provider:web_fetch"),
        # Without the atom, the later checks run as before.
        ({"tools": ["fs_read"], "model_tier": "large"}, {}, ["*"], "tier_unmapped"),
    ],
)
def test_check_order_around_the_atom_refusal(
    create_store, monkeypatch, spec_patch, config_patch, creator_tools, rule
):
    env = create_store
    env[4]["ephemeral"].update(config_patch)
    env[4]["model_tiers"] = {"claude_code": {"small": "claude-small-1"}}
    grant_creator(env, creator_tools)
    for provider in ("claude_code", "codex"):
        narrow(monkeypatch, provider, set(TOOL_ATOMS) - {"web_fetch"})
    spec = {**SPEC, "tools": ["fs_read", "web_fetch"], **spec_patch}
    assert refusal(env, spec).rule == rule


@pytest.fixture
def pending_name(create_store, monkeypatch):
    env = create_store
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    env[4]["ephemeral"]["allowed_providers"] = ["claude_code", "codex"]
    grant_creator(env, ["*"])
    name = create(env, {**SPEC, "provider": "codex", "tools": ["fs_read", "web_fetch"]})["name"]
    tokens = iter(range(1, 100))
    monkeypatch.setattr(env[0].secrets, "token_hex", lambda n: f"{next(tokens):0{n * 2}x}")
    return env, name


def test_claim_collects_a_name_whose_atom_the_provider_dropped(pending_name, monkeypatch):
    env, name = pending_name
    narrow(monkeypatch, "codex", set(TOOL_ATOMS) - {"web_fetch"})
    with pytest.raises(env[0].EphemeralPolicyError) as err:
        env[0].claim_ephemeral_agent(name, {}, CALLER)
    assert err.value.rule == "policy_changed_since_create:tool_unsupported_on_provider:web_fetch"
    assert err.value.detail == (
        "(codex does not support web_fetch in ephemeral agents); re-create the ephemeral agent"
    )
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"
    assert not list((env[2] / "ephemeral" / "live").iterdir())


def test_claim_collects_a_name_whose_provider_was_dropped(pending_name, monkeypatch):
    env, name = pending_name
    env[4]["ephemeral"]["allowed_providers"] = ["claude_code"]
    refuse(monkeypatch, "codex")
    with pytest.raises(env[0].EphemeralPolicyError) as err:
        env[0].claim_ephemeral_agent(name, {}, CALLER)
    assert err.value.rule == "policy_changed_since_create:provider_unsupported"
    assert err.value.detail == "(codex); re-create the ephemeral agent"
    assert database.get_ephemeral_agent(name)["state"] == "gc"


def test_claim_less_bind_runs_the_same_recheck(pending_name, monkeypatch):
    env, name = pending_name
    narrow(monkeypatch, "codex", set(TOOL_ATOMS) - {"web_fetch"})
    with pytest.raises(env[0].EphemeralPolicyError) as err:
        env[0].bind_ephemeral_agent(name, "eeeeeeee", CALLER, "codex", None, None)
    assert err.value.rule == "policy_changed_since_create:tool_unsupported_on_provider:web_fetch"
    assert database.get_ephemeral_agent(name)["state"] == "gc"


def test_claim_with_unchanged_capabilities_still_succeeds(pending_name):
    env, name = pending_name
    result = env[0].claim_ephemeral_agent(name, {}, CALLER)
    assert result["provider"] == "codex" and result["replayed"] is False
    assert result["effective_tools"] == ["fs_read", "web_fetch", "@cao-mcp-server"]


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_honors_model_matches_the_provider_class(create_store, provider):
    env = create_store
    env[4]["ephemeral"]["allowed_providers"] = ["claude_code", "codex"]
    name = create(env, {**SPEC, "provider": provider})["name"]
    profile, source = agent_profiles.load_launch_profile(name)
    assert source is agent_profiles.ProfileSource.EPHEMERAL
    declared = ProviderManager.provider_class(provider).honors_model(name, profile, None)
    assert caps.capability_for(provider).honors_model is declared


@pytest.mark.parametrize("provider", [p.value for p in ProviderType])
def test_rendered_effort_is_exactly_the_capability_mapping(create_store, provider):
    service = create_store[0]
    spec = EphemeralSpec.model_validate({**SPEC})
    tools = ["fs_read", "@cao-mcp-server"]
    with_effort = frontmatter.loads(
        service.render_profile("n", spec, provider, tools, CALLER, effort="high").decode()
    ).metadata
    without = frontmatter.loads(
        service.render_profile("n", spec, provider, tools, CALLER).decode()
    ).metadata
    added = {key: with_effort[key] for key in set(with_effort) - set(without)}
    row = caps.capability_for(provider)
    if row.honors_effort:
        section, key = row.effort_config
        assert added == {section: {key: "high"}}
    else:
        assert added == {}


def test_mcp_schema_advertises_the_table_providers():
    from cli_agent_orchestrator.mcp_server import server

    advertised = server.Tool.from_function(server.create_ephemeral_agent).parameters
    choices = advertised["properties"]["provider"]["anyOf"][0]["enum"]
    assert choices == list(caps.ephemeral_providers())
