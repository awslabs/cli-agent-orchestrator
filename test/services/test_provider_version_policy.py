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


@pytest.mark.parametrize("provider", ["codex", "kimi", "claude", "muse"])
def test_unparseable_versions_remain_fail_closed(provider):
    with pytest.raises(pc.ProviderVersionDrift):
        pc.check_pinned_version(provider, "not-a-version")
