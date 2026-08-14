import pytest

from cli_agent_orchestrator.mcp_server import server


def test_child_profile_policy_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CAO_ALLOWED_CHILD_PROFILES", raising=False)
    server._enforce_child_profile_policy("anything")


def test_child_profile_policy_accepts_allowlisted_profile(monkeypatch):
    monkeypatch.setenv("CAO_ALLOWED_CHILD_PROFILES", "department-worker, shaffer-estimating-a2z")
    server._enforce_child_profile_policy("department-worker")
    server._enforce_child_profile_policy("shaffer-estimating-a2z")


def test_child_profile_policy_rejects_other_profile(monkeypatch):
    monkeypatch.setenv("CAO_ALLOWED_CHILD_PROFILES", "department-worker")
    with pytest.raises(ValueError, match="not allowed"):
        server._enforce_child_profile_policy("developer")
