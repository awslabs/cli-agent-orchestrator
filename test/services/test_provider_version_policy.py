"""Provider-version policy is open by default and strict only by opt-in."""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import provider_contracts as pc


# Derived from the PROVIDERS tuple, not a hand-kept copy of it: a frozen
# literal beside a derived set decays silently, which is exactly how
# antigravity was added to PROVIDERS while receiving none of this coverage.
@pytest.mark.parametrize("provider", sorted(pc.PROVIDERS))
def test_all_providers_admit_future_semver_at_launch_boundary(provider):
    pc.check_pinned_version(provider, "99.99.99")


@pytest.mark.parametrize(
    ("provider", "env_suffix"),
    [(p, p.upper()) for p in sorted(pc.PROVIDERS)],
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


class TestNoCapabilityAllowlists:
    """Capability is proven at runtime; no surface withholds it by build.

    An allowlist of known-good versions excludes every future build by
    default, so it expires the moment the vendor ships and a release
    silently removes a capability nobody chose to give up. Both former
    tables refused all four installed provider builds — codex was the only
    survivor, and only because it happened to be listed. These tests are
    the regression guard against reintroducing either.
    """

    @pytest.mark.parametrize(
        "name",
        [
            "NATIVE_BIND_CAPABLE_VERSIONS",
            "ROUTE_ATTEST_CAPABLE_VERSIONS",
            "is_native_bind_capable",
            "is_route_attest_capable",
        ],
    )
    def test_the_capability_allowlists_are_gone(self, name):
        assert not hasattr(pc, name), (
            f"{name} is a capability allowlist: it answers False for every build "
            "nobody has listed. Prove the contract at runtime instead — see "
            "docs/provider-version-policy.md §1."
        )

    def test_the_bootstrap_carries_no_build_allowlist(self):
        from cli_agent_orchestrator.services import codex_native_bootstrap as cnb

        assert not hasattr(cnb, "BOOTSTRAP_CAPABLE_VERSIONS")
        assert not hasattr(cnb, "is_bootstrap_capable_build")

    def test_the_quarantine_lever_is_untouched(self, monkeypatch):
        """Holding a build back stays possible — that is the sanctioned pin."""
        monkeypatch.setenv("CAO_PROVIDER_VERSION_ENFORCEMENT_CODEX", "strict")
        with pytest.raises(Exception):
            pc.check_pinned_version(pc.PROVIDER_CODEX, "codex-cli 0.999.0")
        monkeypatch.delenv("CAO_PROVIDER_VERSION_ENFORCEMENT_CODEX")
        pc.check_pinned_version(pc.PROVIDER_CODEX, "codex-cli 0.999.0")

    @pytest.mark.parametrize(
        "provider,banner",
        [
            (pc.PROVIDER_CODEX, "codex-cli 0.147.0"),
            (pc.PROVIDER_KIMI, "0.36.1"),
            (pc.PROVIDER_CLAUDE, "2.1.235 (Claude Code)"),
            (pc.PROVIDER_MUSE, "Muse Code 0.2.1 (0.2.1-R1215.1)"),
        ],
    )
    def test_every_installed_build_admits_under_the_open_default(self, provider, banner):
        """The builds actually installed when this landed, none of them listed."""
        pc.check_pinned_version(provider, banner)


def test_antigravity_carries_no_version_pin_at_all():
    """The operator's standing direction: agy must have NO pin, and the live
    installed build must launch.

    Pinning agy is what this asserts against, and it is not hypothetical -- the
    merged branch pinned 1.1.11 while 1.1.13 was installed, and agy auto-updated
    again to 1.1.14 during the session that merged it. A reintroduced pin left
    the whole suite green before this test existed, so the requirement was
    unpinned in both senses of the word.
    """
    assert (
        pc.PROVIDER_ANTIGRAVITY not in pc.PINNED_VERSIONS
    ), "antigravity must carry no reference build; unpinned is the required state"
    assert (
        pc.SUPPORTED_VERSIONS[pc.PROVIDER_ANTIGRAVITY] == ()
    ), "antigravity must list no exact build; a one-element allowlist is an expiry date"
    assert pc.version_enforcement_mode(pc.PROVIDER_ANTIGRAVITY) == pc.VERSION_ENFORCEMENT_OPEN

    # Any semver-shaped build admits, including ones nobody has written down.
    for build in ("1.1.11", "1.1.13", "1.1.14", "99.99.99"):
        pc.check_pinned_version(pc.PROVIDER_ANTIGRAVITY, build)


def test_antigravity_unparseable_banner_still_fails_closed_while_unpinned():
    """Unpinned is not unguarded: a failed observation is distinct from a build
    nobody wrote down, and only the former refuses."""
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version(pc.PROVIDER_ANTIGRAVITY, "not-a-version")
