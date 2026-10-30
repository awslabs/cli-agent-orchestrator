"""#1099: ephemeral.provider_route equivalence sets, their validation and their labels.

Every rule here is provisional (#1099). With today's capability table no non-empty set
validates, so the passing cases substitute capability rows, as #1074's seam tests do: Codex as
sandboxed without web_fetch (the contained launch), and Kiro as hard or advisory.
"""

import itertools
import json
from dataclasses import replace
from test.services.test_ephemeral_claim import NOW, claim
from test.services.test_ephemeral_service import CALLER, SPEC, create, create_store  # noqa: F401

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.ephemeral import TOOL_ATOMS
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps

RULE = "policy_config_error:provider_route"
BOTH = ["claude_code", "codex"]


def sandboxed_codex(monkeypatch):
    """#1074's contained Codex launch: sandboxed, and web_fetch refused."""
    row = replace(
        caps.CAPABILITIES["codex"],
        enforcement=caps.SANDBOXED,
        tool_atoms=frozenset(TOOL_ATOMS) - {"web_fetch"},
    )
    monkeypatch.setitem(caps.CAPABILITIES, "codex", row)


def enabled_kiro(monkeypatch, enforcement=caps.HARD):
    """#1167 Phase 2's Kiro row, or a second advisory provider. It is never constructed here."""
    row = caps.EphemeralCapability(
        enforcement, frozenset(TOOL_ATOMS), True, ("kiroConfig", "effort")
    )
    monkeypatch.setitem(caps.CAPABILITIES, "kiro_cli", row)


def declare(env, sets, **block):
    env[4]["ephemeral"]["allowed_providers"] = BOTH
    env[4]["ephemeral"]["provider_route"] = {"equivalent": sets, **block}


def refused(env):
    with pytest.raises(env[0].EphemeralPolicyError) as err:
        env[0].validate_block(env[0].read_settings())
    return err.value


def test_no_sets_is_the_default_and_validates(create_store):
    service = create_store[0]
    settings = service.read_settings()
    assert settings["provider_route"] == {"equivalent": []}
    service.validate_block(settings)
    assert "provider_route" not in settings["_unknown_keys"]


# AC6: with the real table, every non-empty set refuses.


def test_AC6_claude_code_with_advisory_codex_refuses_under_the_real_table(create_store):
    declare(create_store, [BOTH])
    error = refused(create_store)
    assert error.rule == RULE
    assert "mixes advisory with hard or sandboxed" in error.detail
    assert "claude_code=hard" in error.detail and "codex=advisory" in error.detail


@pytest.mark.parametrize("sets", [[["claude_code", "kiro_cli"]], [["codex", "kiro_cli"]]])
def test_AC6_any_set_naming_kiro_refuses_under_the_real_table(create_store, sets):
    declare(create_store, sets)
    error = refused(create_store)
    assert error.rule == RULE and "supported providers: claude_code, codex" in error.detail


def test_inert_today_every_set_of_two_or_more_refuses_under_the_real_table(create_store):
    """The docs' claim: v1 routes nothing until a capability row changes (#1099)."""
    providers = caps.ephemeral_providers()
    assert providers == ("claude_code", "codex")
    candidates = [
        list(members)
        for size in range(2, len(providers) + 2)
        for members in itertools.combinations(providers + ("kiro_cli",), size)
    ]
    assert candidates
    for members in candidates:
        for order in itertools.permutations(members):
            declare(create_store, [list(order)])
            assert refused(create_store).rule == RULE, order


# AC6 with substituted rows: hard and sandboxed may mix; advisory only with advisory.


def test_AC6_hard_with_sandboxed_validates(create_store, monkeypatch):
    sandboxed_codex(monkeypatch)
    declare(create_store, [BOTH])
    create_store[0].validate_block(create_store[0].read_settings())


def test_AC6_hard_with_hard_validates(create_store, monkeypatch):
    enabled_kiro(monkeypatch)
    declare(create_store, [["claude_code", "kiro_cli"]])
    create_store[0].validate_block(create_store[0].read_settings())


def test_AC6_advisory_with_advisory_validates(create_store, monkeypatch):
    enabled_kiro(monkeypatch, caps.ADVISORY)
    declare(create_store, [["codex", "kiro_cli"]])
    create_store[0].validate_block(create_store[0].read_settings())


@pytest.mark.parametrize("kiro", [caps.HARD, caps.SANDBOXED])
def test_AC6_advisory_with_hard_or_sandboxed_refuses(create_store, monkeypatch, kiro):
    enabled_kiro(monkeypatch, kiro)
    declare(create_store, [["codex", "kiro_cli"]])
    error = refused(create_store)
    assert error.rule == RULE and "codex=advisory" in error.detail


def test_two_disjoint_sets_validate(create_store, monkeypatch):
    sandboxed_codex(monkeypatch)
    enabled_kiro(monkeypatch, caps.ADVISORY)
    monkeypatch.setitem(
        caps.CAPABILITIES,
        "mock_cli",
        caps.EphemeralCapability(caps.ADVISORY, frozenset(), True, ("mockConfig", "effort")),
    )
    declare(create_store, [BOTH, ["kiro_cli", "mock_cli"]])
    create_store[0].validate_block(create_store[0].read_settings())


def test_overlapping_sets_refuse_even_when_each_set_is_valid(create_store, monkeypatch):
    sandboxed_codex(monkeypatch)
    enabled_kiro(monkeypatch)
    declare(create_store, [BOTH, ["codex", "kiro_cli"]])
    error = refused(create_store)
    assert error.rule == RULE and "overlaps an earlier set" in error.detail
    # Each set validates on its own: only the overlap refuses.
    for sets in ([BOTH], [["codex", "kiro_cli"]]):
        declare(create_store, sets)
        create_store[0].validate_block(create_store[0].read_settings())


# The shape refusals, at create and at claim.

SHAPES = [
    ("block", None),
    ("block", []),
    ("block", "claude_code"),
    ("block", {}),
    ("block", {"equivalent": [], "sets": []}),
    ("block", {"equivalents": []}),
    ("sets", None),
    ("sets", {"claude_code": "codex"}),
    ("sets", "claude_code,codex"),
    ("sets", ["claude_code", "codex"]),
    ("sets", [BOTH, "codex"]),
    ("sets", [["claude_code"]]),
    ("sets", [[]]),
    ("sets", [["claude_code", "claude_code"]]),
    ("sets", [["claude_code", 5]]),
    ("sets", [["claude_code", None]]),
    ("sets", [["claude_code", ["codex"]]]),
    ("sets", [["claude_code", {"provider": "codex"}]]),
    ("sets", [["claude_code", "Codex"]]),
    ("sets", [["claude_code", "bogus"]]),
    ("sets", [["claude_code", "codex"], ["codex", "kiro_cli"]]),
]


def _shape(env, kind, value):
    env[4]["ephemeral"]["allowed_providers"] = BOTH
    env[4]["ephemeral"]["provider_route"] = value if kind == "block" else {"equivalent": value}


@pytest.mark.parametrize("kind,value", SHAPES)
def test_a_malformed_block_refuses_every_create(create_store, monkeypatch, kind, value):
    # With hard plus sandboxed and an advisory Kiro, only the shape itself can refuse.
    sandboxed_codex(monkeypatch)
    enabled_kiro(monkeypatch, caps.ADVISORY)
    _shape(create_store, kind, value)
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create(create_store)
    assert err.value.rule == RULE
    assert err.value.detail.startswith("ephemeral.provider_route")
    assert not list((create_store[2] / "ephemeral").glob("live/*"))


@pytest.mark.parametrize("kind,value", SHAPES[:6] + SHAPES[11:15])
def test_a_block_broken_after_create_refuses_the_claim_and_returns_the_name(
    create_store, monkeypatch, kind, value
):
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    name = create(create_store)["name"]
    _shape(create_store, kind, value)
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        claim(create_store, name)
    assert err.value.rule == RULE
    assert "the operator must fix ephemeral.provider_route" in err.value.detail
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "pending" and row["claim_id"] is None


def test_members_need_not_be_allowed_providers(create_store, monkeypatch):
    sandboxed_codex(monkeypatch)
    declare(create_store, [BOTH])
    create_store[4]["ephemeral"]["allowed_providers"] = ["claude_code"]
    create_store[0].validate_block(create_store[0].read_settings())


# One label helper: create's enforcement, the validation and (at launch) the records.


@pytest.mark.parametrize("provider", [p.value for p in ProviderType])
def test_the_label_helper_is_the_capability_class(provider):
    assert caps.enforcement_label(provider) == caps.capability_for(provider).enforcement
    assert caps.enforcement_label(provider) in caps.ENFORCEMENT_CLASSES


def test_create_and_validation_read_the_one_label_helper(create_store, monkeypatch):
    """A label that comes to depend on settings or an engine version changes in one place."""
    service = create_store[0]
    declare(create_store, [BOTH])
    assert refused(create_store).rule == RULE
    seen = []

    def relabel(provider):
        seen.append(provider)
        return caps.SANDBOXED if provider == "codex" else caps.enforcement_label(provider)

    tokens = iter(range(1, 100))
    monkeypatch.setattr(service.secrets, "token_hex", lambda n: f"{next(tokens):0{n * 2}x}")
    with monkeypatch.context() as patch:
        patch.setattr(service, "enforcement_label", relabel)
        service.validate_block(service.read_settings())
        assert set(seen) >= set(BOTH)
        assert create(create_store, {**SPEC, "provider": "codex"})["enforcement"] == "sandboxed"
        assert create(create_store, {**SPEC, "provider": "claude_code"})["enforcement"] == "hard"


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
def test_create_reports_the_label_helper_for_each_supported_provider(create_store, provider):
    assert set(caps.ephemeral_providers()) == {"claude_code", "codex"}
    create_store[4]["ephemeral"]["allowed_providers"] = BOTH
    created = create(create_store, {**SPEC, "provider": provider})
    assert created["enforcement"] == caps.enforcement_label(provider)


# provider "auto": create resolves the creator's provider as the fallback (#1099).

BOTH_TIERS = {
    provider: {t: f"{provider}-{t}" for t in ("small", "medium", "large")}
    for provider in ("claude_code", "codex")
}
AUTO = {**SPEC, "provider": "auto"}


def routable(env, monkeypatch, sets=None, **ephemeral):
    """Codex sandboxed, both providers allowed and mapped, and one declared set."""
    sandboxed_codex(monkeypatch)
    env[4]["model_tiers"] = BOTH_TIERS
    declare(env, [BOTH] if sets is None else sets)
    env[4]["ephemeral"].update(ephemeral)


def test_an_auto_spec_falls_back_to_the_creators_provider(create_store, monkeypatch):
    routable(create_store, monkeypatch, default_tier="medium")
    result = create(create_store, AUTO)
    assert result["provider"] == "claude_code" and result["enforcement"] == "hard"
    assert result["notes"][-1] == (
        "provider: auto (decided at launch within ephemeral.provider_route.equivalent; "
        "fallback claude_code, the creator's provider)"
    )
    row = database.get_ephemeral_agent(result["name"])
    assert row["provider"] == "claude_code"
    live = create_store[2] / "ephemeral/live"
    stored = json.loads((live / (result["name"] + ".spec.json")).read_bytes())
    assert stored["provider"] == "auto"
    profile = (live / (result["name"] + ".md")).read_text()
    assert "provider: claude_code" in profile and "model: claude_code-medium" in profile


def test_an_explicit_spec_gains_no_provider_note(create_store, monkeypatch):
    routable(create_store, monkeypatch)
    notes = create(create_store, {**SPEC, "provider": "codex"})["notes"]
    assert len(notes) == 2 and not any(note.startswith("provider:") for note in notes)


def test_an_auto_spec_from_an_unsupported_creator_says_set_provider(create_store, monkeypatch):
    routable(create_store, monkeypatch)
    with create_store[1]() as db:
        db.get(database.TerminalModel, CALLER).provider = "kiro_cli"
        db.commit()
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create(create_store, AUTO)
    assert err.value.rule == "provider_unsupported"
    assert err.value.detail == "kiro_cli; set provider explicitly"


def test_the_spec_rejects_an_unknown_provider_but_accepts_auto():
    from cli_agent_orchestrator.models.ephemeral import EphemeralSpec

    assert EphemeralSpec.model_validate(AUTO).provider == "auto"
    assert EphemeralSpec.model_validate(AUTO).model_dump(mode="json")["provider"] == "auto"
    for value in ("Auto", "bogus", 5, ["auto"]):
        with pytest.raises(ValueError):
            EphemeralSpec.model_validate({**SPEC, "provider": value})


# The lease covers one more on-mode wait for provider auto.


@pytest.mark.parametrize(
    "spec,lease,refused",
    [
        (AUTO, 35, False),
        (AUTO, 34, True),
        ({**AUTO, "model_tier": "auto"}, 40, False),
        ({**AUTO, "model_tier": "auto"}, 39, True),
    ],
)
def test_the_lease_floor_counts_the_provider_wait(create_store, monkeypatch, spec, lease, refused):
    routable(
        create_store,
        monkeypatch,
        min_tier="small",
        default_tier="medium",
        max_tier="large",
        claim_lease_seconds=lease,
    )
    # 5000 ms, the setting's maximum: the floor is waits * 5 + 30 seconds.
    create_store[4]["decisions"] = {"on_timeout_ms": 5000}
    if not refused:
        create(create_store, spec)
        return
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create(create_store, spec)
    assert err.value.rule == "policy_config_error:claim_lease_seconds"
    assert "provider=auto" in err.value.detail
    if "model_tier" in spec:
        assert err.value.detail.endswith("for provider=auto and model_tier=auto")


def test_the_lease_floor_is_rechecked_at_claim(create_store, monkeypatch):
    routable(create_store, monkeypatch)
    create_store[4]["decisions"] = {"on_timeout_ms": 5000}
    name = create(create_store, AUTO)["name"]
    create_store[4]["ephemeral"]["claim_lease_seconds"] = 34
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        claim(create_store, name)
    assert err.value.rule == "policy_config_error:claim_lease_seconds"
    assert database.get_ephemeral_agent(name)["state"] == "pending"


# The options: CAO's filter, in table order, always with the fallback.


def options(env, name, spec=None):
    service = env[0]
    row = database.get_ephemeral_agent(name)
    stored = service._stored_spec(row) if spec is None else spec
    return service.provider_options(row, stored, service.read_settings())


def test_options_hold_every_member_that_passes_and_their_labels(create_store, monkeypatch):
    routable(create_store, monkeypatch, default_tier="medium", default_effort="medium")
    name = create(create_store, AUTO)["name"]
    result = options(create_store, name)
    assert result.options == ("claude_code", "codex")
    assert dict(result.labels) == {"claude_code": "hard", "codex": "sandboxed"}
    assert dict(result.labels) == {p: caps.enforcement_label(p) for p in result.options}


def test_no_declared_set_leaves_only_the_fallback(create_store, monkeypatch):
    routable(create_store, monkeypatch, sets=[])
    name = create(create_store, AUTO)["name"]
    assert options(create_store, name).options == ("claude_code",)


def test_a_member_not_allowed_is_dropped(create_store, monkeypatch):
    routable(create_store, monkeypatch)
    create_store[4]["ephemeral"]["allowed_providers"] = ["claude_code"]
    name = create(create_store, AUTO)["name"]
    assert options(create_store, name).options == ("claude_code",)


def test_a_member_that_cannot_run_a_stored_atom_is_dropped(create_store, monkeypatch):
    routable(create_store, monkeypatch)
    with create_store[1]() as db:
        db.get(database.TerminalModel, CALLER).allowed_tools = json.dumps(
            ["@cao-mcp-server", "fs_read", "web_fetch"]
        )
        db.commit()
    name = create(create_store, {**AUTO, "tools": ["fs_read", "web_fetch"]})["name"]
    assert options(create_store, name).options == ("claude_code",)
    plain = create(create_store, {**AUTO, "purpose": "log_review"})["name"]
    assert options(create_store, plain).options == ("claude_code", "codex")


@pytest.mark.parametrize(
    "tier,codex_table,kept",
    [
        ("large", {"small": "codex-small", "medium": "codex-medium"}, False),
        ("medium", {"small": "codex-small", "medium": "codex-medium"}, True),
        ("auto", {"small": "codex-small", "medium": "codex-medium"}, False),
        (None, {}, True),
    ],
)
def test_a_member_that_cannot_map_a_reachable_tier_is_dropped(
    create_store, monkeypatch, tier, codex_table, kept
):
    routable(create_store, monkeypatch)
    bounds = {"min_tier": "small", "default_tier": "small", "max_tier": "large"}
    if tier == "auto":
        create_store[4]["ephemeral"].update(bounds)
    spec = {**AUTO, **({"model_tier": tier} if tier else {})}
    name = create(create_store, spec)["name"]
    # The table changes after create, as an operator edit would.
    create_store[4]["model_tiers"] = {**BOTH_TIERS, "codex": codex_table}
    if tier == "auto":
        # A valid block requires every allowed provider to map the bounds: drop Codex from
        # the allowed list, which the filter's first rule then refuses on its own.
        create_store[4]["ephemeral"]["allowed_providers"] = ["claude_code"]
    expected = ("claude_code", "codex") if kept else ("claude_code",)
    assert options(create_store, name).options == expected


def test_the_reachable_tiers_of_an_auto_tier_are_the_bounds(create_store, monkeypatch):
    service = create_store[0]
    routable(create_store, monkeypatch, min_tier="small", default_tier="small", max_tier="medium")
    name = create(create_store, {**AUTO, "model_tier": "auto"})["name"]
    row = database.get_ephemeral_agent(name)
    spec = service._stored_spec(row)
    settings = service.read_settings()
    seen = []
    original = service._route_refusal

    def spy(row, settings, provider, *, tiers, effort):
        seen.append(list(tiers))
        return original(row, settings, provider, tiers=tiers, effort=effort)

    monkeypatch.setattr(service, "_route_refusal", spy)
    service.provider_options(row, spec, settings)
    assert seen == [["small", "medium"]]


@pytest.mark.parametrize("effort", ["medium", None])
def test_a_member_that_cannot_apply_an_effort_is_dropped_only_when_one_launches(
    create_store, monkeypatch, effort
):
    from types import SimpleNamespace

    routable(create_store, monkeypatch, default_effort=effort)
    # dataclasses.replace re-runs the capability table's invariant (#1074), so only a stub
    # can drop effort support.
    stub = SimpleNamespace(
        enforcement=caps.SANDBOXED,
        tool_atoms=frozenset(TOOL_ATOMS),
        honors_model=True,
        effort_config=None,
        supports_ephemeral=True,
        honors_effort=False,
    )
    monkeypatch.setitem(caps.CAPABILITIES, "codex", stub)
    name = create(create_store, AUTO)["name"]
    expected = ("claude_code",) if effort else ("claude_code", "codex")
    assert options(create_store, name).options == expected


def test_only_the_fallbacks_set_is_offered(create_store, monkeypatch):
    sandboxed_codex(monkeypatch)
    enabled_kiro(monkeypatch)
    create_store[4]["model_tiers"] = {
        **BOTH_TIERS,
        "kiro_cli": {t: "kiro-" + t for t in ("small", "medium", "large")},
    }
    create_store[4]["ephemeral"].update(
        allowed_providers=["claude_code", "codex", "kiro_cli"],
        provider_route={"equivalent": [["claude_code", "kiro_cli"]]},
    )
    name = create(create_store, AUTO)["name"]
    # Codex is allowed and supported, but not in Claude Code's set.
    assert options(create_store, name).options == ("claude_code", "kiro_cli")


def test_a_member_of_another_set_is_never_offered(create_store, monkeypatch):
    sandboxed_codex(monkeypatch)
    enabled_kiro(monkeypatch)
    monkeypatch.setitem(
        caps.CAPABILITIES,
        "mock_cli",
        caps.EphemeralCapability(caps.SANDBOXED, frozenset(TOOL_ATOMS), True, ("m", "effort")),
    )
    providers = ["claude_code", "codex", "kiro_cli", "mock_cli"]
    create_store[4]["model_tiers"] = {
        p: {t: f"{p}-{t}" for t in ("small", "medium", "large")} for p in providers
    }
    create_store[4]["ephemeral"].update(
        allowed_providers=providers,
        provider_route={"equivalent": [["claude_code", "kiro_cli"], ["codex", "mock_cli"]]},
    )
    name = create(create_store, AUTO)["name"]
    # Every provider is allowed and could run the launch; only the fallback's own set counts.
    assert options(create_store, name).options == ("claude_code", "kiro_cli")


ORDER_PROBE = r"""
import json, sys
from dataclasses import replace
from cli_agent_orchestrator.models.ephemeral import EphemeralSpec, TOOL_ATOMS
from cli_agent_orchestrator.services import ephemeral_service as service
from cli_agent_orchestrator.utils import ephemeral_capabilities as caps

row = caps.EphemeralCapability(caps.HARD, frozenset(TOOL_ATOMS), True, ("c", "effort"))
caps.CAPABILITIES["codex"] = replace(caps.CAPABILITIES["codex"], enforcement=caps.SANDBOXED)
caps.CAPABILITIES["kiro_cli"] = row
caps.CAPABILITIES["mock_cli"] = row
members = json.loads(sys.argv[1])
settings = {
    "allowed_providers": members,
    "provider_route": {"equivalent": [members]},
    "_model_tiers": {p: {"medium": p + "-m"} for p in members},
}
spec = EphemeralSpec.model_validate({"purpose": "log_triage", "brief": "b", "provider": "auto"})
db_row = {"provider": "claude_code", "effective_tools": ["fs_read"], "model_tier": "medium",
          "effort": None}
print(json.dumps(service.provider_options(db_row, spec, settings).options))
"""


def test_options_are_in_table_order_under_any_hash_seed_and_set_order(tmp_path):
    """Set members and the survivors' set order vary; the options never do (#1099)."""
    import os
    import subprocess
    import sys

    members = ["claude_code", "codex", "kiro_cli", "mock_cli"]
    results = set()
    for seed in range(8):
        for order in itertools.islice(itertools.permutations(members), 0, None, 5):
            env = {
                **{k: v for k, v in os.environ.items() if not k.startswith(("CAO_", "HERDR_"))},
                "PYTHONHASHSEED": str(seed),
                "HOME": str(tmp_path),
                "CAO_HOME_DIR": str(tmp_path / "cao"),
            }
            out = subprocess.run(
                [sys.executable, "-c", ORDER_PROBE, json.dumps(list(order))],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            ).stdout
            results.add(tuple(json.loads(out.strip().splitlines()[-1])))
    assert results == {("claude_code", "codex", "kiro_cli", "mock_cli")}


# decision_inputs asks provider.route first, and only for an auto spec (AC7).


def test_decision_inputs_ask_provider_first_with_cao_options(create_store, monkeypatch):
    routable(create_store, monkeypatch, min_tier="small", default_tier="medium", max_tier="large")
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    name = create(create_store, {**AUTO, "model_tier": "auto"})["name"]
    token = claim(create_store, name)["claim_id"]
    inputs = create_store[0].decision_inputs(name, token, CALLER, "claude_code")
    assert list(inputs.field_states) == ["provider.route", "model.route"]
    assert inputs.provider_options.options == ("claude_code", "codex")
    # provider.route has no default; only the declared tier's fallback is replaced.
    assert inputs.policy.default_tier == "medium"


def test_decision_inputs_for_a_provider_only_auto_spec(create_store, monkeypatch):
    routable(create_store, monkeypatch)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    name = create(create_store, AUTO)["name"]
    token = claim(create_store, name)["claim_id"]
    inputs = create_store[0].decision_inputs(name, token, CALLER, "claude_code")
    assert inputs.field_states == {"provider.route": "auto"}


@pytest.mark.parametrize("provider", ["codex", "claude_code", None])
def test_an_explicit_or_omitted_provider_asks_nothing(create_store, monkeypatch, provider):
    routable(create_store, monkeypatch)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    spec = {**SPEC, **({"provider": provider} if provider else {})}
    name = create(create_store, spec)["name"]
    response = claim(create_store, name)
    assert response["provider_auto"] is False
    row = database.get_ephemeral_agent(name)
    assert (
        create_store[0].decision_inputs(name, response["claim_id"], CALLER, row["provider"]) is None
    )


# The pre-bind recheck of a decided provider: the claim's rules, against the current policy.


def _claimed(env, monkeypatch, spec=AUTO, **ephemeral):
    routable(env, monkeypatch, **ephemeral)
    monkeypatch.setattr(database, "_utcnow", lambda: NOW)
    name = create(env, spec)["name"]
    return name, claim(env, name)["claim_id"]


def test_the_recheck_maps_the_stored_tier_and_effort_for_the_decided_provider(
    create_store, monkeypatch
):
    name, token = _claimed(
        create_store, monkeypatch, default_tier="medium", default_effort="medium"
    )
    decided = create_store[0].recheck_decided(name, token, CALLER, None, provider="codex")
    assert (decided.model, decided.effort) == ("codex-medium", "medium")
    unrouted = create_store[0].recheck_decided(name, token, CALLER, None)
    assert unrouted.model == "claude_code-medium"


def test_the_recheck_reverse_maps_an_unrecorded_tier_with_the_decided_table(
    create_store, monkeypatch
):
    name, token = _claimed(
        create_store,
        monkeypatch,
        spec={**AUTO, "model_tier": "auto"},
        min_tier="small",
        default_tier="medium",
        max_tier="large",
    )
    decided = create_store[0].recheck_decided(
        name, token, CALLER, None, None, "codex-large", provider="codex"
    )
    assert decided.model == "codex-large"


@pytest.mark.parametrize(
    "change,rule",
    [
        ({"provider_route": {"equivalent": []}}, "provider_not_equivalent"),
        ({"allowed_providers": ["claude_code"]}, "provider_not_allowed"),
        ({"model_tiers": {**BOTH_TIERS, "codex": {"small": "codex-small"}}}, "tier_unmapped"),
    ],
)
def test_a_policy_change_during_the_decision_collects_the_name(
    create_store, monkeypatch, change, rule
):
    name, token = _claimed(create_store, monkeypatch, spec={**AUTO, "model_tier": "large"})
    if "model_tiers" in change:
        create_store[4]["model_tiers"] = change["model_tiers"]
    else:
        create_store[4]["ephemeral"].update(change)
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create_store[0].recheck_decided(name, token, CALLER, None, provider="codex")
    assert err.value.rule == "policy_changed_since_create:" + rule
    row = database.get_ephemeral_agent(name)
    assert row["state"] == "gc" and row["gc_reason"] == "policy_changed"


def test_the_recheck_still_requires_the_stored_fallback(create_store, monkeypatch):
    name, token = _claimed(create_store, monkeypatch)
    create_store[4]["ephemeral"]["allowed_providers"] = ["codex"]
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create_store[0].recheck_decided(name, token, CALLER, None, provider="codex")
    assert err.value.rule == "policy_changed_since_create:provider_not_allowed"


def test_the_recheck_bounds_a_decided_effort_for_the_decided_provider(create_store, monkeypatch):
    name, token = _claimed(
        create_store,
        monkeypatch,
        spec={**AUTO, "effort": "auto"},
        min_effort="low",
        default_effort="medium",
        max_effort="high",
    )
    create_store[4]["ephemeral"].update(max_effort="medium")
    with pytest.raises(create_store[0].EphemeralPolicyError) as err:
        create_store[0].recheck_decided(name, token, CALLER, None, "high", provider="codex")
    assert err.value.rule == "policy_changed_since_create:effort_exceeds_policy"
