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
    assert pc.validate_resume_argv("claude", ["--resume", "uuid-1"]).native_id == "uuid-1"


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
    codex = pc.resume_status("codex")
    assert codex.identity_available and not codex.authority_supported
    claude = pc.resume_status("claude")
    assert claude.identity_available and not claude.authority_supported
    kimi = pc.resume_status("kimi")
    assert not kimi.identity_available and not kimi.authority_supported
    kimi_proven = pc.resume_status("kimi", kimi_acp_proof_green=True)
    assert kimi_proven.identity_available and not kimi_proven.authority_supported


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
    assert payload["resume"]["codex"]["identity_available"] is True
    assert payload["resume"]["codex"]["authority_supported"] is False
    assert payload["resume"]["kimi"]["identity_available"] is False
    assert payload["resource_registry_version"] == 1
    assert payload["delivery_journal"]["at_most_once_honest"] is True
    assert "cao-w13-fence-receipt-v1" in payload["receipts"]


def test_capability_claims_derive_from_composition_not_config():
    composition = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=_receipt(),
        deployment_generation=3,
    )
    payload = build_capabilities(containment=composition, kimi_acp_proof_green=True)
    assert payload["containment"] == "proven"
    assert payload["resume"]["kimi"]["identity_available"] is True
    # A dead extension (no live receipt) reports unproven regardless.
    dead = ContainmentComposition(
        authorization=_authorization(),
        live_proof_receipt=None,
        deployment_generation=3,
    )
    assert build_capabilities(containment=dead)["containment"] == "unproven"
