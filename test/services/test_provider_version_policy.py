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
