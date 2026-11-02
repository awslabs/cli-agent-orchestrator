"""Cause-specific, path-free plan-identity refusal contract."""

import pytest

from cli_agent_orchestrator.services import approval_gate, manifest_freeze
from cli_agent_orchestrator.utils import git_baseline

REASONS = (
    "untracked_too_large",
    "untracked_unsupported",
    "repository_unreadable",
    "git_unavailable",
    "git_timeout",
    "changed_during_snapshot",
    "internal",
    "no_recorded_identity",
    "profiles_unreadable",
)
RETRY = {"git_timeout", "changed_during_snapshot", "internal"}


@pytest.mark.parametrize("reason", REASONS)
def test_identity_reason_selects_fixed_three_key_message(monkeypatch, tmp_path, reason):
    from cli_agent_orchestrator.api.main import _approval_refusal_detail

    monkeypatch.setattr(approval_gate, "is_workflow_approval_required", lambda: True)
    with pytest.raises(approval_gate.PlanIdentityUnavailableError) as refused:
        approval_gate.ensure_plan_approved(
            tier="script", manifest_json=None, unavailable_reason=reason
        )
    assert refused.value.reason == reason
    detail = _approval_refusal_detail(refused.value)
    assert set(detail) == {"kind", "plan_id", "message"}
    assert detail["kind"] == "plan_identity_unavailable"
    assert detail["plan_id"] is None
    assert detail["message"] == str(refused.value)
    assert ("Retry" in detail["message"]) == (reason in RETRY)
    assert str(tmp_path) not in detail["message"]
    assert "require_approval" not in detail["message"]
    assert "CAO_WORKFLOW" not in detail["message"]
    if reason == "untracked_too_large":
        assert "64 MiB" in detail["message"] and "gitignore" in detail["message"].lower()
    if reason == "no_recorded_identity":
        assert "start a new run" in detail["message"].lower()


def test_reason_set_is_closed():
    assert git_baseline.UNAVAILABLE_REASONS == frozenset(REASONS)


def test_internal_freeze_failure_logs_only_reason(monkeypatch, caplog, tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError(f"private error at {tmp_path}")

    monkeypatch.setattr(manifest_freeze, "derive_baseline", fail)
    with caplog.at_level("WARNING"):
        result = manifest_freeze.freeze_manifest(source_hash="source", inputs={}, cwd=str(tmp_path))
    assert result.manifest_json is None
    assert result.unavailable_reason == "internal"
    assert str(tmp_path) not in caplog.text
    assert "internal" in caplog.text


def test_unknown_reason_is_not_echoed(monkeypatch, tmp_path):
    monkeypatch.setattr(approval_gate, "is_workflow_approval_required", lambda: True)
    with pytest.raises(approval_gate.PlanIdentityUnavailableError) as refused:
        approval_gate.ensure_plan_approved(
            tier="script", manifest_json=None, unavailable_reason=str(tmp_path)
        )
    assert refused.value.reason == "internal"
    assert str(tmp_path) not in str(refused.value)


def test_profile_capture_reason_keeps_path_free_message(monkeypatch, tmp_path):
    from cli_agent_orchestrator.services import launch_guard

    monkeypatch.setattr(
        launch_guard.settings_service, "is_workflow_approval_required", lambda: True
    )

    def fail():
        raise OSError(str(tmp_path))

    monkeypatch.setattr(launch_guard.agent_profiles, "list_agent_profiles", fail)
    with pytest.raises(approval_gate.PlanIdentityUnavailableError) as refused:
        launch_guard.capture()
    assert refused.value.reason == "profiles_unreadable"
    assert (
        str(refused.value)
        == "Could not read the agent profile set for this script run, so its approval cannot be bound."
    )
    assert str(tmp_path) not in str(refused.value)
