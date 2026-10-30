"""#1099: ephemeral.provider_route equivalence sets, their validation and their labels.

Every rule here is provisional (#1099). With today's capability table no non-empty set
validates, so the passing cases substitute capability rows, as #1074's seam tests do: Codex as
sandboxed without web_fetch (the contained launch), and Kiro as hard or advisory.
"""

import itertools
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
