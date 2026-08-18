"""Tests for provider contracts, containment interfaces, and capabilities
(T-PROV / T-CAP-1..3 / T-PF-2-shape fork side)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import provider_contracts as pc
from cli_agent_orchestrator.services import recovery_capabilities
from cli_agent_orchestrator.services.containment import (
    ArtifactAuthorization,
    ContainmentComposition,
    ContainmentUnproven,
    validate_proof_receipt,
)
from cli_agent_orchestrator.services.recovery_capabilities import build_capabilities

# ------------------------------------------------------- provider contracts


def test_pinned_versions():
    # The pin is advisory: the representative head of each provider's
    # accepted tuple, never a ceiling.  The pairing is asserted in both
    # directions so the two maps cannot silently drift apart, and the
    # baseline is derived from the tables rather than frozen beside them —
    # a hand-maintained copy here would be a gate that decays.
    for provider, pin in pc.PINNED_VERSIONS.items():
        assert pin in pc.SUPPORTED_VERSIONS[provider]
        assert pc.SUPPORTED_VERSIONS[provider][0] == pin
    # Open mode (the default for every provider): an unlisted semver-shaped
    # build launches, listed or not, current or future.
    pc.check_pinned_version("codex", "0.146.0")
    pc.check_pinned_version("codex", "codex-cli 0.146.0")
    pc.check_pinned_version("codex", "codex-cli 0.145.0")
    pc.check_pinned_version("codex", "codex-cli 0.146.1")
    pc.check_pinned_version("kimi", "0.28.0")
    pc.check_pinned_version("kimi", "0.99.0")


def test_strict_mode_quarantines_an_unlisted_build_at_the_launch_boundary(monkeypatch):
    """The one place listing still gates: the opt-in quarantine."""
    monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI", "strict")
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", "0.99.0")
    # The listed builds still pass under strict quarantine.
    pc.check_pinned_version("kimi", pc.PINNED_VERSIONS["kimi"])


@pytest.mark.parametrize("mode", ["open", "strict"])
def test_an_unparseable_banner_fails_closed_in_every_mode(monkeypatch, mode):
    """Unparseable is a failed observation — distinct from unlisted, which
    is merely nothing written down."""
    monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI", mode)
    for rejected in ("kimi", "", "not-a-version"):
        with pytest.raises(pc.ProviderVersionDrift):
            pc.check_pinned_version("kimi", rejected)


def test_kimi_accepts_both_pinned_and_retained_versions_but_never_a_range():
    # Exact-set acceptance for *proven builds*: 0.34.0 is the current pin
    # (cond-0331: open enforcement admits it and every future semver at the
    # launch identity boundary, while the exact set below still gates
    # feature-specific authority), 0.33.0 (cond-0315), 0.32.0 (cond-0315),
    # 0.31.0 (cond-0310), 0.30.0 (cond-0198) and 0.29.2/0.29.1/0.29.0 are
    # retained for already-minted sessions.  Nothing outside this set may
    # inherit feature-specific authority — this is a set of proven builds,
    # not a version range.
    assert pc.SUPPORTED_VERSIONS["kimi"] == (
        "0.34.0",
        "0.33.0",
        "0.32.0",
        "0.31.0",
        "0.30.0",
        "0.29.2",
        "0.29.1",
        "0.29.0",
    )
    pc.check_pinned_version("kimi", "kimi 0.34.0")
    pc.check_pinned_version("kimi", "kimi 0.33.0")
    pc.check_pinned_version("kimi", "kimi 0.32.0")
    pc.check_pinned_version("kimi", "kimi 0.31.0")
    pc.check_pinned_version("kimi", "kimi 0.30.0")
    pc.check_pinned_version("kimi", "kimi 0.29.2")
    pc.check_pinned_version("kimi", "kimi 0.29.1")
    pc.check_pinned_version("kimi", "kimi 0.29.0")
    # Open enforcement: semver-shaped versions outside the proven set are
    # accepted at the launch identity boundary, but they do not inherit
    # feature-specific authority (control/resume/image/steer/composer).
    pc.check_pinned_version("kimi", "0.28.0")
    pc.check_pinned_version("kimi", "0.35.0")
    pc.check_pinned_version("kimi", "1.29.0")
    # Malformed or unparseable versions still fail closed at the boundary.
    for rejected in ("kimi", "", "not-a-version"):
        with pytest.raises(pc.ProviderVersionDrift):
            pc.check_pinned_version("kimi", rejected)
    # The current pin is always an accepted build.
    assert pc.PINNED_VERSIONS["kimi"] in pc.SUPPORTED_VERSIONS["kimi"]


def test_normalized_version_extracts_the_semver_token():
    assert pc.normalized_version("kimi 0.29.1") == "0.29.1"
    assert pc.normalized_version("2.1.220 (Claude Code)") == "2.1.220"
    assert pc.normalized_version("no version here") == ""


def test_kimi_enforcement_mode_is_open_by_default():
    assert all(
        pc.version_enforcement_mode(provider) == pc.VERSION_ENFORCEMENT_OPEN
        for provider in pc.PROVIDERS
    )


def test_kimi_can_be_reverted_to_strict_by_environment_variable(monkeypatch):
    monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI", "strict")
    assert pc.version_enforcement_mode("kimi") == pc.VERSION_ENFORCEMENT_STRICT
    # In strict mode, versions outside the exact proven set are refused.
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", "0.35.0")
    pc.check_pinned_version("kimi", "0.34.0")


@pytest.mark.parametrize("provider", pc.PROVIDERS)
def test_any_provider_can_be_reverted_to_strict(monkeypatch, provider):
    monkeypatch.setenv(f"CAO_PROVIDER_VERSION_ENFORCEMENT_{provider.upper()}", "strict")
    assert pc.version_enforcement_mode(provider) == pc.VERSION_ENFORCEMENT_STRICT
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version(provider, "99.99.99")


def test_native_id_sources():
    assert pc.native_id_source("codex") == "app_server_thread_start"
    assert pc.native_id_source("kimi") == "acp_session_new"
    assert pc.native_id_source("claude") == "cli_session_id"


def test_exact_resume_forms_accepted():
    assert pc.validate_resume_argv("codex", ["resume", "thr_1"]).native_id == "thr_1"
    assert pc.validate_resume_argv("codex", ["exec", "resume", "thr_1"]).native_id == "thr_1"
    # kimi: ``--session`` golden, ``-S`` documented short form, ``-r`` the
    # bundle-verified hidden compatibility alias — all exact-id forms.
    assert pc.validate_resume_argv("kimi", ["--session", "session_abc"]).native_id == "session_abc"
    assert pc.validate_resume_argv("kimi", ["-S", "session_abc"]).native_id == "session_abc"
    assert pc.validate_resume_argv("kimi", ["-r", "session_abc"]).native_id == "session_abc"
    claude = pc.validate_resume_argv("claude", ["--resume", "11111111-1111-4111-8111-111111111111"])
    assert claude.native_id == "11111111-1111-4111-8111-111111111111"


def test_claude_resume_native_id_must_be_canonical_uuid():
    # PROV-2 durable regression: Claude's native session id is a canonical
    # UUID; any other shape is refused, never resumed blindly.
    with pytest.raises(pc.ResumeFormRefused):
        pc.validate_resume_argv("claude", ["--resume", "not-a-native-uuid"])
    with pytest.raises(pc.ResumeFormRefused):
        pc.validate_resume_argv("claude", ["--resume", "11111111-1111-4111-8111-11111111111Z"])
    with pytest.raises(pc.ResumeFormRefused):
        # non-canonical (uppercase) rendering
        pc.validate_resume_argv("claude", ["--resume", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"])


@pytest.mark.parametrize(
    "provider,argv",
    [
        ("codex", ["resume", "--last"]),
        ("codex", ["--ephemeral"]),
        ("codex", ["resume"]),
        ("kimi", ["--continue"]),
        ("kimi", ["-c"]),
        ("kimi", ["--session"]),
        ("kimi", ["-S"]),
        ("kimi", ["-r"]),
        ("claude", ["--continue"]),
        ("claude", ["--fork-session", "x"]),
        ("claude", ["--no-session-persistence"]),
        ("claude", ["--resume"]),
    ],
)
def test_forbidden_resume_forms_refused(provider, argv):
    with pytest.raises(pc.ResumeFormRefused):
        pc.validate_resume_argv(provider, argv)


def test_resume_status_truthful_defaults():
    # Without a live version fact every provider fails closed: no identity,
    # no authority (an absent or drifted binary removes the capability).
    codex = pc.resume_status("codex")
    assert not codex.identity_available and not codex.authority_supported
    claude = pc.resume_status("claude")
    assert not claude.identity_available and not claude.authority_supported
    kimi = pc.resume_status("kimi")
    assert not kimi.identity_available and not kimi.authority_supported


def test_resume_status_version_checked_and_receipt_bound():
    # An observed binary — listed or not — carries resume identity (never
    # authority by itself); a failed version observation removes it
    # (outcome 41 semantics).
    codex = pc.resume_status("codex", installed_version="codex 0.146.0")
    assert codex.identity_available and not codex.authority_supported
    # Unlisted is merely nothing written down: identity follows the
    # observation, not the quarantine set.
    unlisted = pc.resume_status("codex", installed_version="codex 0.146.1")
    assert unlisted.identity_available
    # Unparseable is a failed observation: no identity.
    drifted = pc.resume_status("codex", installed_version="codex not-a-version")
    assert not drifted.identity_available
    claude = pc.resume_status("claude", installed_version="2.1.220 (Claude Code)")
    assert claude.identity_available and not claude.authority_supported
    unlisted_claude = pc.resume_status("claude", installed_version="2.1.216 (Claude Code)")
    assert unlisted_claude.identity_available
    assert not pc.resume_status("claude", installed_version="(Claude Code)").identity_available
    # Kimi identity additionally requires the validated durable ACP proof.
    kimi_unproven = pc.resume_status("kimi", installed_version="kimi 0.29.0")
    assert not kimi_unproven.identity_available
    kimi_proven = pc.resume_status(
        "kimi", installed_version="kimi 0.29.0", kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"}
    )
    assert kimi_proven.identity_available and not kimi_proven.authority_supported
    # An unlisted Kimi build with the durable ACP proof has identity too:
    # the receipt is bound to the installed binary's digest, not to a row
    # in a table.
    kimi_unlisted_proven = pc.resume_status(
        "kimi", installed_version="kimi 0.99.0", kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"}
    )
    assert kimi_unlisted_proven.identity_available
    # A provider-specific route receipt promotes ONLY that provider's authority.
    codex_route = pc.resume_status(
        "codex", installed_version="codex 0.146.0", route_proof=_valid_route_proof("codex")
    )
    assert codex_route.authority_supported
    # An unvalidated/foreign/echo route object never promotes authority.
    for bad_proof in (
        {"schema": "route-receipt"},
        _valid_route_proof("kimi"),
        {**_valid_route_proof("codex"), "non_echo": False},
        {**_valid_route_proof("codex"), "observed_effort": ""},
    ):
        status = pc.resume_status("codex", installed_version="codex 0.146.0", route_proof=bad_proof)
        assert status.identity_available and not status.authority_supported


def test_route_proof_typed_validation_and_pinned_binding():
    """cond-0069 closure: malformed or drifted receipts expose no authority.

    The validator requires typed session/turn/generation identity, a
    positive event sequence, a 64-hex model-input digest, and a resolved
    route equal to the pinned expectation — never mere key presence.
    """
    complete = _valid_route_proof("codex")
    assert pc.validate_route_proof("codex", complete)
    # Structurally malformed evidence never validates.
    for mutation in (
        {"native_turn_id": {}},
        {"native_turn_id": ""},
        {"native_session_id": 17},
        {"generation": None},
        {"event_sequence": -1},
        {"event_sequence": 0},
        {"event_sequence": "7"},
        {"event_sequence": True},
        {"model_input_digest": "not-a-digest"},
        {"model_input_digest": "D" * 64},
        {"model_input_digest": "d" * 63},
        {"protocol_version": ""},
        {"observed_model": "different-model"},
        {"observed_effort": "different-effort"},
    ):
        assert not pc.validate_route_proof("codex", {**complete, **mutation}), mutation
    # Missing pinned expectation: no authority.
    without_pin = {k: v for k, v in complete.items() if k not in ("expected_model",)}
    assert not pc.validate_route_proof("codex", without_pin)
    # The authority boundary's expectation binds observed model/effort and
    # the model-input digest; drift against it never validates.
    assert pc.validate_route_proof(
        "codex",
        complete,
        expected_model="gpt-5.6-sol",
        expected_effort="max",
        expected_model_input_digest="d" * 64,
    )
    assert not pc.validate_route_proof("codex", complete, expected_model="different-model")
    assert not pc.validate_route_proof("codex", complete, expected_effort="different-effort")
    assert not pc.validate_route_proof("codex", complete, expected_model_input_digest="e" * 64)
    # Foreign providers never validate for this provider.
    assert not pc.validate_route_proof("codex", _valid_route_proof("kimi"))


def _valid_route_proof(provider: str) -> dict:
    """A schema-valid cao-route-receipt-v1 for the given provider."""
    return {
        "schema": "cao-route-receipt-v1",
        "provider": provider,
        "native_session_id": "native-session-1",
        "native_turn_id": "native-turn-1",
        "generation": "gen-000042",
        "observed_model": "gpt-5.6-sol",
        "observed_effort": "max",
        "expected_model": "gpt-5.6-sol",
        "expected_effort": "max",
        "protocol_version": "app-server/1",
        "event_sequence": 7,
        "model_input_digest": "d" * 64,
        "non_echo": True,
    }


# ------------------------------------------------------------- containment


def test_no_authorization_always_unproven():
    composition = ContainmentComposition()
    assert composition.status() == "unproven"
    with pytest.raises(ContainmentUnproven):
        composition.require_proven("report finalization step 7")
    with pytest.raises(ContainmentUnproven):
        composition.revoke_generation("gen-000042")


def _authorization():
    return ArtifactAuthorization(
        repository="cao-containment-ext",
        extension_sha256="e" * 64,
        manager_sha256="m" * 64,
        proof_issuer="pf1a-proof-issuer",
        authorized_at="2026-07-23T12:00:00Z",
    )


def _receipt(**changes):
    receipt = {
        "schema": "cao-containment-proof-v1",
        "extension_sha256": "e" * 64,
        "manager_sha256": "m" * 64,
        "proof_issuer": "pf1a-proof-issuer",
        "deployment_generation": 3,
        "proof_matrix_id": "T-PF-1b",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5))
        .isoformat()
        .replace("+00:00", "Z"),
    }
    receipt.update(changes)
    return receipt


def test_valid_live_receipt_proves():
    composition = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=_receipt(),
        deployment_generation=3,
    )
    assert composition.status() == "proven"
    composition.require_proven("gated path")


def test_receipt_mismatches_unproven():
    for change in (
        {"extension_sha256": "0" * 64},
        {"manager_sha256": "0" * 64},
        {"proof_issuer": "someone-else"},
        {"deployment_generation": 2},
        {"proof_matrix_id": ""},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"schema": "cao-containment-proof-v0"},
    ):
        with pytest.raises(ContainmentUnproven):
            validate_proof_receipt(
                _receipt(**change),
                authorization=_authorization(),
                deployment_generation=3,
            )


def test_absent_authorization_rejects_any_receipt():
    with pytest.raises(ContainmentUnproven, match="authorization"):
        validate_proof_receipt(_receipt(), authorization=None, deployment_generation=3)


# ------------------------------------------------------------ capabilities


def test_zero_proven_providers_advertised_truthfully():
    payload = build_capabilities()
    assert payload["schema_version"] == 1
    assert payload["containment"] == "unproven"
    assert payload["observed_route"] == {
        "codex": "unsupported",
        "claude": "unsupported",
        "kimi": "unproven",
    }
    # Zero proven providers: no enabled provider and every automated
    # recovery/finalization/destructive path is unavailable.
    assert payload["enabled_providers"] == []
    assert payload["automated_paths"] == {
        "recovery": False,
        "finalization": False,
        "destructive": False,
    }
    assert payload["resume"]["codex"]["identity_available"] is False
    assert payload["resume"]["codex"]["authority_supported"] is False
    assert payload["resume"]["kimi"]["identity_available"] is False
    assert payload["resource_registry_version"] == 1
    assert payload["delivery_journal"]["at_most_once_honest"] is True
    assert "cao-w13-fence-receipt-v1" in payload["receipts"]
    assert payload["callback_recovery"]["providers"] == []
    assert payload["callback_recovery"]["enabled"] is False


def test_capability_claims_derive_from_receipts_never_caller_booleans():
    # CAP-2 durable regression: a provider's observed-route claim derives
    # only from that provider's own version-checked receipt — one receipt
    # promotes exactly one provider, and there is no global caller boolean.
    composition = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=_receipt(),
        deployment_generation=3,
    )
    payload = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex 0.146.0", "kimi": "kimi 0.29.0"},
        kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"},
        route_proofs={"codex": _valid_route_proof("codex")},
    )
    assert payload["containment"] == "proven"
    assert payload["observed_route"] == {
        "codex": "proven",  # only Codex carries a validated route receipt
        "claude": "unsupported",
        "kimi": "unproven",
    }
    assert payload["resume"]["kimi"]["identity_available"] is True
    # Claude's binary was never version-verified: no identity.
    assert payload["resume"]["claude"]["identity_available"] is False
    # Identity alone enables nothing: Kimi has identity without route
    # authority, so only Codex is enabled and bears the automated paths.
    assert payload["enabled_providers"] == ["codex"]
    assert payload["automated_paths"]["recovery"] is True
    # Unknown/missing/unsupported route evidence exposes no automated path
    # even with containment proven and exact pinned versions.
    identity_only = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex 0.146.0", "kimi": "kimi 0.29.0"},
        kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"},
    )
    assert identity_only["resume"]["codex"]["identity_available"] is True
    assert identity_only["enabled_providers"] == []
    assert identity_only["automated_paths"] == {
        "recovery": False,
        "finalization": False,
        "destructive": False,
    }
    # An unvalidated route object (wrong schema, foreign provider, echo, or
    # missing fields) is treated as absent.
    for bad_proof in (
        {"schema": "route-receipt"},
        _valid_route_proof("kimi"),
        {**_valid_route_proof("codex"), "non_echo": False},
        {**_valid_route_proof("codex"), "observed_model": None},
    ):
        unproven = build_capabilities(
            containment=composition,
            provider_versions={"codex": "codex 0.146.0"},
            route_proofs={"codex": bad_proof},
        )
        assert unproven["observed_route"]["codex"] == "unsupported"
        assert unproven["enabled_providers"] == []
        assert unproven["automated_paths"]["recovery"] is False
    # An unlisted build keeps the capability its receipts prove: the route
    # receipt binds the generation and the journaled digest, not a table
    # row.  A *failed* version observation removes it.
    unlisted = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex 0.146.1", "kimi": "kimi 0.29.0"},
        kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"},
        route_proofs={"codex": _valid_route_proof("codex")},
    )
    assert unlisted["resume"]["codex"]["identity_available"] is True
    assert unlisted["resume"]["codex"]["authority_supported"] is True
    drifted = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex not-a-version", "kimi": "kimi 0.29.0"},
        kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"},
        route_proofs={"codex": _valid_route_proof("codex")},
    )
    assert drifted["resume"]["codex"]["identity_available"] is False
    assert drifted["resume"]["codex"]["authority_supported"] is False
    # The authority boundary's pinned route binds the receipt: a mismatching
    # expectation exposes no authority even for a well-formed receipt.
    pinned = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex 0.146.0"},
        route_proofs={"codex": _valid_route_proof("codex")},
        route_expectations={
            "codex": {"model": "gpt-5.6-sol", "effort": "max", "model_input_digest": "d" * 64}
        },
    )
    assert pinned["enabled_providers"] == ["codex"]
    for drifted_expectation in (
        {"model": "different-model", "effort": "max"},
        {"model": "gpt-5.6-sol", "effort": "different-effort"},
        {"model": "gpt-5.6-sol", "effort": "max", "model_input_digest": "e" * 64},
    ):
        refused = build_capabilities(
            containment=composition,
            provider_versions={"codex": "codex 0.146.0"},
            route_proofs={"codex": _valid_route_proof("codex")},
            route_expectations={"codex": drifted_expectation},
        )
        assert refused["enabled_providers"] == []
        assert refused["observed_route"]["codex"] == "unsupported"
    # A dead extension (no live receipt) reports unproven regardless.
    dead = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=None,
        deployment_generation=3,
    )
    assert build_capabilities(containment=dead)["containment"] == "unproven"


def test_callback_capability_is_the_proven_outer_authority_intersection(monkeypatch):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "yes")
    monkeypatch.setattr(database, "callback_recovery_migration_ready", lambda: True)
    composition = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=_receipt(),
        deployment_generation=3,
    )
    payload = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex 0.146.0", "kimi": "kimi 0.29.0"},
        kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"},
        route_proofs={"codex": _valid_route_proof("codex")},
    )
    assert payload["enabled_providers"] == ["codex"]
    assert payload["callback_recovery"]["providers"] == ["codex"]
    assert payload["callback_recovery"]["enabled"] is True


def test_callback_capability_and_admission_stay_disabled_when_migration_is_unready(monkeypatch):
    monkeypatch.setenv("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "yes")
    monkeypatch.setattr(database, "callback_recovery_migration_ready", lambda: False)
    composition = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=_receipt(),
        deployment_generation=3,
    )
    payload = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex 0.146.0"},
        route_proofs={"codex": _valid_route_proof("codex")},
    )

    assert payload["callback_recovery"]["providers"] == ["codex"]
    assert payload["callback_recovery"]["enabled"] is False
    monkeypatch.setattr(recovery_capabilities, "build_capabilities", lambda: payload)
    assert recovery_capabilities.callback_recovery_admission_allowed("codex") is False
