"""Provider-version policy is open by default and strict only by opt-in."""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import provider_contracts as pc


@pytest.mark.parametrize("provider", ["codex", "kimi", "claude", "muse"])
def test_all_providers_admit_future_semver_at_launch_boundary(provider):
    pc.check_pinned_version(provider, "99.99.99")


@pytest.mark.parametrize(
    ("provider", "env_suffix"),
    [("codex", "CODEX"), ("kimi", "KIMI"), ("claude", "CLAUDE"), ("muse", "MUSE")],
)
def test_all_providers_can_restore_strict_exact_enforcement(monkeypatch, provider, env_suffix):
    monkeypatch.setenv(f"CAO_PROVIDER_VERSION_ENFORCEMENT_{env_suffix}", "strict")
    assert pc.version_enforcement_mode(provider) == pc.VERSION_ENFORCEMENT_STRICT
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version(provider, "99.99.99")


@pytest.mark.parametrize(
    ("wire_provider", "env_suffix"),
    [
        ("codex", "CODEX"),
        ("kimi_cli", "KIMI"),
        ("claude_code", "CLAUDE"),
        ("muse_cli", "MUSE"),
    ],
)
def test_wire_provider_uses_documented_short_name_override(monkeypatch, wire_provider, env_suffix):
    """Managed-launch wire keys must honor the public short-name setting."""
    monkeypatch.setenv(f"CAO_PROVIDER_VERSION_ENFORCEMENT_{env_suffix}", "strict")
    assert pc.version_enforcement_mode(wire_provider) == pc.VERSION_ENFORCEMENT_STRICT


def test_wire_name_override_remains_compatible(monkeypatch):
    """Older wire-key environment settings remain deterministic."""
    monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI_CLI", "strict")
    assert pc.version_enforcement_mode("kimi_cli") == pc.VERSION_ENFORCEMENT_STRICT


def test_short_name_override_wins_over_legacy_wire_name(monkeypatch):
    monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI", "open")
    monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI_CLI", "strict")
    assert pc.version_enforcement_mode("kimi_cli") == pc.VERSION_ENFORCEMENT_OPEN


@pytest.mark.parametrize("wire_provider", ["codex", "kimi_cli", "claude_code", "muse_cli"])
def test_wire_provider_inherits_open_default_without_override(monkeypatch, wire_provider):
    """Wire identifiers must not silently fall back to strict mode."""
    for suffix in ("CODEX", "KIMI", "CLAUDE", "MUSE", "KIMI_CLI", "CLAUDE_CODE", "MUSE_CLI"):
        monkeypatch.delenv(f"CAO_PROVIDER_VERSION_ENFORCEMENT_{suffix}", raising=False)
    assert pc.version_enforcement_mode(wire_provider) == pc.VERSION_ENFORCEMENT_OPEN


@pytest.mark.parametrize("provider", ["codex", "kimi", "claude", "muse"])
def test_unparseable_versions_remain_fail_closed(provider):
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version(provider, "not-a-version")


class TestNativeBindCapability:
    """The narrow build capability the managed native bind seam consults.

    Reproduced live: a real Codex CLI 0.147.0 managed native launch
    completed the zero-turn bootstrap, exposed the exact provider session
    identity, reported input_ready — and was refused at bind because the
    seam consulted the broad ``SUPPORTED_VERSIONS`` table instead of the
    capability the launch had actually proven.
    """

    def test_the_stage_proven_codex_builds_bind(self):
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, "codex-cli 0.146.0")
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, "codex-cli 0.147.0")

    @pytest.mark.parametrize(
        "banner", ["codex-cli 0.148.0", "codex-cli 0.147", "codex-cli unknown", ""]
    )
    def test_every_unproven_codex_build_fails_closed(self, banner):
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, banner) is False

    def test_an_absent_version_is_not_a_capability(self):
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, None) is False
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, 0.147) is False

    def test_the_capability_disagrees_with_the_broad_table_in_both_directions(self):
        """0.147.0 holds the narrow proof and not the broad one.

        The disagreement is the design, so both directions are pinned:
        the narrow table accepts a build the broad table refuses, and the
        broad table itself is unchanged — this repair must not reach it.
        """
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is True
        assert pc.is_listed_version(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is False
        assert pc.SUPPORTED_VERSIONS[pc.PROVIDER_CODEX] == ("0.146.0",)
        # And the shared builds keep both predicates.
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, "codex-cli 0.146.0") is True
        assert pc.is_listed_version(pc.PROVIDER_CODEX, "codex-cli 0.146.0") is True

    @pytest.mark.parametrize("provider", ["kimi", "claude", "muse"])
    def test_other_providers_keep_exactly_their_broad_proven_set(self, provider):
        """No provider's bind behaviour changed except Codex's.

        Their native identity paths were verified with each accepted build,
        so their cells are the broad tuples by reference: every broadly
        proven build binds, nothing else does, and adding a build to the
        broad table carries bind for them exactly as before.
        """
        assert pc.NATIVE_BIND_CAPABLE_VERSIONS[provider] == pc.SUPPORTED_VERSIONS[provider]
        for build in pc.SUPPORTED_VERSIONS[provider]:
            assert pc.is_native_bind_capable(provider, build) is True
        assert pc.is_native_bind_capable(provider, "99.99.99") is False

    def test_strict_enforcement_does_not_widen_the_seam(self, monkeypatch):
        """The seam ignores the mode — and the opt-in pin still refuses 0.147.

        The capability predicates answer a different question than launch
        admission, so a strict override cannot widen bind; conversely the
        strict override keeps refusing the unlisted build at the launch
        boundary, exactly as before.
        """
        monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_CODEX", "strict")
        assert pc.is_native_bind_capable(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is True
        assert pc.is_listed_version(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is False
        with pytest.raises(pc.ProviderVersionDrift):
            pc.check_pinned_version(pc.PROVIDER_CODEX, "codex-cli 0.147.0")

    def test_bind_capability_grants_identity_but_not_route_authority(self):
        """Bind capability grants no route authority by implication.

        Resume identity follows the version observation under the unpinned
        policy, so the stage-proven 0.147.0 has it; automated
        recovery/strongest-route authority still requires a
        model-input-bound non-echo route receipt, which nothing here
        supplies.
        """
        status = pc.resume_status(pc.PROVIDER_CODEX, installed_version="codex-cli 0.147.0")
        assert status.identity_available is True
        assert status.authority_supported is False

    def test_a_failed_version_observation_has_no_identity(self):
        """Unparseable is a failed observation, not an unlisted build."""
        status = pc.resume_status(pc.PROVIDER_CODEX, installed_version="codex-cli unknown")
        assert status.identity_available is False
        assert status.authority_supported is False


class TestRouteAttestCapability:
    """The narrow build set the zero-task trust/route-attestation seam uses.

    Reproduced live: a Codex CLI 0.147.0 install re-arms the launch breaker
    through ``managed-launch/attest-route`` — the app-server exchange is
    the same initialize/config/read/thread-start(ephemeral)/no-turn surface
    the native-bind seam stage-verified — but the route attestor refused it
    with a single-version banner constant (409 "expected codex-cli
    0.146.0, observed 0.147.0"). The repair follows the native-bind
    design: a capability table in its own right inside
    ``provider_contracts``, consulted independently of launch-enforcement
    mode, never a casual widening of ``SUPPORTED_VERSIONS``.
    """

    def test_the_route_attestation_builds_are_capable(self):
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, "codex-cli 0.146.0")
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, "codex-cli 0.147.0")

    @pytest.mark.parametrize(
        "banner", ["codex-cli 0.148.0", "codex-cli 0.147", "codex-cli unknown", ""]
    )
    def test_every_unproven_codex_build_fails_closed(self, banner):
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, banner) is False

    def test_an_absent_version_is_not_a_capability(self):
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, None) is False
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, 0.147) is False

    def test_the_capability_disagrees_with_the_broad_table_in_both_directions(self):
        """0.147.0 holds the narrow attestation proof and not the broad one.

        The route-attestation repair must not reach the broad table, so
        both directions are pinned: the narrow table accepts a build the
        broad table refuses, and ``SUPPORTED_VERSIONS`` itself is
        unchanged.
        """
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is True
        assert pc.is_listed_version(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is False
        assert pc.SUPPORTED_VERSIONS[pc.PROVIDER_CODEX] == ("0.146.0",)
        assert pc.NATIVE_BIND_CAPABLE_VERSIONS[pc.PROVIDER_CODEX] == ("0.146.0", "0.147.0")
        # And the shared builds keep both predicates.
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, "codex-cli 0.146.0") is True
        assert pc.is_listed_version(pc.PROVIDER_CODEX, "codex-cli 0.146.0") is True

    @pytest.mark.parametrize("provider", ["kimi", "claude", "muse"])
    def test_other_providers_keep_exactly_their_broad_proven_set(self, provider):
        """No provider's route-attestation admission changed except Codex's.

        Kimi's ACP and Claude's version-pinned route probes were verified
        with each accepted build, so their cells are the broad tuples by
        reference: every broadly proven build attests, nothing else does.
        """
        assert pc.ROUTE_ATTEST_CAPABLE_VERSIONS[provider] == pc.SUPPORTED_VERSIONS[provider]
        for build in pc.SUPPORTED_VERSIONS[provider]:
            assert pc.is_route_attest_capable(provider, build) is True
        assert pc.is_route_attest_capable(provider, "99.99.99") is False

    def test_strict_enforcement_does_not_widen_the_seam(self, monkeypatch):
        """The predicate ignores the launch mode — exactly like native bind.

        A strict override refuses the unlisted build at the launch
        boundary while the narrow capability predicate keeps admitting the
        stage-proven 0.147.0 attestation.
        """
        monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_CODEX", "strict")
        assert pc.is_route_attest_capable(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is True
        assert pc.is_listed_version(pc.PROVIDER_CODEX, "codex-cli 0.147.0") is False
        with pytest.raises(pc.ProviderVersionDrift):
            pc.check_pinned_version(pc.PROVIDER_CODEX, "codex-cli 0.147.0")

    def test_attestation_capability_grants_identity_but_not_route_authority(self):
        """Attestation capability grants no route authority by implication.

        Resume identity follows the version observation under the unpinned
        policy, so the attestation-proven 0.147.0 has it; automated
        recovery/strongest-route authority still requires a
        model-input-bound non-echo route receipt, which nothing here
        supplies.
        """
        status = pc.resume_status(pc.PROVIDER_CODEX, installed_version="codex-cli 0.147.0")
        assert status.identity_available is True
        assert status.authority_supported is False
