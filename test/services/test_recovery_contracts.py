"""Tests for provider contracts, containment interfaces, and capabilities
(T-PROV / T-CAP-1..3 / T-PF-2-shape fork side)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from cli_agent_orchestrator.services import provider_contracts as pc
from cli_agent_orchestrator.services.containment import (
    ArtifactAuthorization,
    ContainmentComposition,
    ContainmentUnproven,
    validate_proof_receipt,
)
from cli_agent_orchestrator.services.recovery_capabilities import build_capabilities

# ------------------------------------------------------- provider contracts


def test_pinned_versions():
    assert pc.PINNED_VERSIONS == {"codex": "0.145.0", "kimi": "0.29.0", "claude": "2.1.218"}
    pc.check_pinned_version("codex", "0.145.0")
    pc.check_pinned_version("codex", "codex-cli 0.145.0")
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("codex", "codex-cli 0.144.6")
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version("kimi", "0.28.0")


def test_native_id_sources():
    assert pc.native_id_source("codex") == "app_server_thread_start"
    assert pc.native_id_source("kimi") == "acp_session_new"
    assert pc.native_id_source("claude") == "cli_session_id"


def test_exact_resume_forms_accepted():
    assert pc.validate_resume_argv("codex", ["resume", "thr_1"]).native_id == "thr_1"
    assert pc.validate_resume_argv("codex", ["exec", "resume", "thr_1"]).native_id == "thr_1"
    assert pc.validate_resume_argv("kimi", ["--session", "session_abc"]).native_id == "session_abc"
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
    # A version-matched binary restores resume identity (never authority);
    # version drift removes it again (outcome 41 semantics).
    codex = pc.resume_status("codex", installed_version="codex 0.145.0")
    assert codex.identity_available and not codex.authority_supported
    drifted = pc.resume_status("codex", installed_version="codex 0.145.1")
    assert not drifted.identity_available
    claude = pc.resume_status("claude", installed_version="2.1.218 (Claude Code)")
    assert claude.identity_available and not claude.authority_supported
    drifted_claude = pc.resume_status("claude", installed_version="2.1.216 (Claude Code)")
    assert not drifted_claude.identity_available
    # Kimi identity additionally requires the validated durable ACP proof.
    kimi_unproven = pc.resume_status("kimi", installed_version="kimi 0.29.0")
    assert not kimi_unproven.identity_available
    kimi_proven = pc.resume_status(
        "kimi", installed_version="kimi 0.29.0", kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"}
    )
    assert kimi_proven.identity_available and not kimi_proven.authority_supported
    # A provider-specific route receipt promotes ONLY that provider's authority.
    codex_route = pc.resume_status(
        "codex", installed_version="codex 0.145.0", route_proof=_valid_route_proof("codex")
    )
    assert codex_route.authority_supported
    # An unvalidated/foreign/echo route object never promotes authority.
    for bad_proof in (
        {"schema": "route-receipt"},
        _valid_route_proof("kimi"),
        {**_valid_route_proof("codex"), "non_echo": False},
        {**_valid_route_proof("codex"), "observed_effort": ""},
    ):
        status = pc.resume_status("codex", installed_version="codex 0.145.0", route_proof=bad_proof)
        assert status.identity_available and not status.authority_supported


def _valid_route_proof(provider: str) -> dict:
    """A schema-valid cao-route-receipt-v1 for the given provider."""
    return {
        "schema": "cao-route-receipt-v1",
        "provider": provider,
        "native_session_id": "native-session-1",
        "native_turn_id": "native-turn-1",
        "observed_model": "gpt-5.6-sol",
        "observed_effort": "max",
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
        provider_versions={"codex": "codex 0.145.0", "kimi": "kimi 0.29.0"},
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
        provider_versions={"codex": "codex 0.145.0", "kimi": "kimi 0.29.0"},
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
            provider_versions={"codex": "codex 0.145.0"},
            route_proofs={"codex": bad_proof},
        )
        assert unproven["observed_route"]["codex"] == "unsupported"
        assert unproven["enabled_providers"] == []
        assert unproven["automated_paths"]["recovery"] is False
    # Runtime version drift removes the capability.
    drifted = build_capabilities(
        containment=composition,
        provider_versions={"codex": "codex 0.145.1", "kimi": "kimi 0.29.0"},
        kimi_acp_proof={"schema": "cao-kimi-acp-proof-v1"},
        route_proofs={"codex": _valid_route_proof("codex")},
    )
    assert drifted["resume"]["codex"]["identity_available"] is False
    assert drifted["resume"]["codex"]["authority_supported"] is False
    # A dead extension (no live receipt) reports unproven regardless.
    dead = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=None,
        deployment_generation=3,
    )
    assert build_capabilities(containment=dead)["containment"] == "unproven"
