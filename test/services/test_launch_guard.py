"""Tests for the narrow per-run profile and memory drift guard."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import launch_guard


def test_capture_is_absent_when_approval_is_disabled(monkeypatch):
    monkeypatch.setattr(
        launch_guard.settings_service,
        "is_workflow_approval_required",
        lambda: False,
    )

    assert launch_guard.capture() is None


def test_check_rejects_profile_and_memory_drift(monkeypatch):
    guard = {
        "profiles": {"worker": "sha256:frozen"},
        "memory_enabled": False,
    }
    row = SimpleNamespace(
        tier="script",
        spec_snapshot=json.dumps({"launch_guard": guard}),
    )
    monkeypatch.setattr(launch_guard.workflow_journal, "get_run", lambda run_id: row)
    monkeypatch.setattr(launch_guard, "_profile_digest", lambda name: "sha256:changed")
    monkeypatch.setattr(launch_guard.settings_service, "is_memory_enabled", lambda: False)

    with pytest.raises(launch_guard.PlanInputsChangedError, match="profile.*changed"):
        launch_guard.check("run-1", "worker")

    monkeypatch.setattr(launch_guard, "_profile_digest", lambda name: "sha256:frozen")
    monkeypatch.setattr(launch_guard.settings_service, "is_memory_enabled", lambda: True)
    with pytest.raises(launch_guard.PlanInputsChangedError, match="memory setting changed"):
        launch_guard.check("run-1", "worker")


def test_check_ignores_yaml_and_legacy_script_rows(monkeypatch):
    rows = {
        "yaml": SimpleNamespace(tier="yaml", spec_snapshot="{}"),
        "legacy": SimpleNamespace(tier="script", spec_snapshot="steps: []"),
    }
    monkeypatch.setattr(launch_guard.workflow_journal, "get_run", rows.get)

    launch_guard.check("yaml", "worker")
    launch_guard.check("legacy", "worker")
