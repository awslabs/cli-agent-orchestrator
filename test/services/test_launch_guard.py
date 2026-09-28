"""Tests for the narrow per-run profile and memory drift guard."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.services import launch_guard


def _posture(required: bool, source: str | None = None):
    return launch_guard.settings_service.WorkflowApprovalPosture(
        required,
        source or launch_guard.settings_service.GATE_SOURCE_FILE,
    )


def _script_row(snapshot):
    return SimpleNamespace(tier="script", spec_snapshot=snapshot)


def test_capture_writes_disabled_approval_marker(monkeypatch):
    monkeypatch.setattr(
        launch_guard.settings_service,
        "is_workflow_approval_required",
        lambda: False,
    )

    assert launch_guard.capture() == {"approval_required": False}


@pytest.mark.parametrize(
    "snapshot",
    [
        json.dumps({"launch_guard": {"approval_required": False}}),
        json.dumps({"launch_guard": None}),
        json.dumps({}),
        json.dumps({"launch_guard": []}),
        "steps: []",
    ],
    ids=["marker", "null", "missing", "non-dict", "non-json"],
)
def test_check_approval_off_returns_before_journal_read(monkeypatch, snapshot):
    reads = []
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: _posture(False),
    )
    monkeypatch.setattr(
        launch_guard.workflow_journal,
        "get_run",
        lambda run_id: reads.append(run_id) or _script_row(snapshot),
    )

    launch_guard.check("run-1", "worker")

    assert reads == []


def test_check_rejects_profile_and_memory_drift(monkeypatch):
    guard = {
        "profiles": {"worker": "sha256:frozen"},
        "memory_enabled": False,
    }
    row = _script_row(json.dumps({"launch_guard": guard}))
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: _posture(True),
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


def test_valid_drifted_guard_passes_after_approval_flips_off(monkeypatch):
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: _posture(False),
    )
    monkeypatch.setattr(
        launch_guard.workflow_journal,
        "get_run",
        lambda run_id: pytest.fail("approval-off checks must not read the journal"),
    )
    monkeypatch.setattr(
        launch_guard,
        "_profile_digest",
        lambda name: pytest.fail("approval-off checks must not digest profiles"),
    )

    launch_guard.check("run-1", "worker")


def test_check_ignores_yaml_but_rejects_unreadable_script_rows(monkeypatch):
    rows = {
        "yaml": SimpleNamespace(tier="yaml", spec_snapshot="{}"),
        "legacy": _script_row("steps: []"),
    }
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: _posture(True),
    )
    monkeypatch.setattr(launch_guard.workflow_journal, "get_run", rows.get)

    launch_guard.check("yaml", "worker")
    with pytest.raises(launch_guard.PlanInputsChangedError, match="unreadable"):
        launch_guard.check("legacy", "worker")


def test_check_rejects_disabled_marker_when_approval_is_now_on(monkeypatch):
    row = _script_row(json.dumps({"launch_guard": {"approval_required": False}}))
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: _posture(True),
    )
    monkeypatch.setattr(launch_guard.workflow_journal, "get_run", lambda run_id: row)

    with pytest.raises(
        launch_guard.PlanInputsChangedError,
        match="approval enforcement was turned on after this run started; start a new run",
    ):
        launch_guard.check("run-1", "worker")


@pytest.mark.parametrize(
    "snapshot",
    [
        json.dumps({"launch_guard": None}),
        json.dumps({}),
        json.dumps({"launch_guard": []}),
        "not json",
    ],
    ids=["null", "missing", "non-dict", "non-json"],
)
def test_check_rejects_unreadable_launch_state_when_approval_is_on(monkeypatch, snapshot):
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: _posture(True),
    )
    monkeypatch.setattr(
        launch_guard.workflow_journal,
        "get_run",
        lambda run_id: _script_row(snapshot),
    )

    with pytest.raises(launch_guard.PlanInputsChangedError, match="unreadable"):
        launch_guard.check("run-1", "worker")


def test_check_retries_read_failure_then_reports_settings_repair(monkeypatch):
    resolutions = [
        _posture(True, launch_guard.settings_service.GATE_SOURCE_READ_FAILURE),
        _posture(True, launch_guard.settings_service.GATE_SOURCE_READ_FAILURE),
    ]
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: resolutions.pop(0),
    )
    monkeypatch.setattr(
        launch_guard.workflow_journal,
        "get_run",
        lambda run_id: pytest.fail("settings failure must stop before journal reads"),
    )

    with pytest.raises(launch_guard.PlanInputsChangedError) as exc:
        launch_guard.check("run-1", "worker")

    assert "approval setting could not be read" in str(exc.value)
    assert "repair settings.json and then resume" in str(exc.value)
    assert "changed" not in str(exc.value)
    assert resolutions == []


def test_check_read_failure_retry_can_observe_approval_off(monkeypatch):
    resolutions = [
        _posture(True, launch_guard.settings_service.GATE_SOURCE_READ_FAILURE),
        _posture(False),
    ]
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: resolutions.pop(0),
    )
    monkeypatch.setattr(
        launch_guard.workflow_journal,
        "get_run",
        lambda run_id: pytest.fail("approval-off retry must stop before journal reads"),
    )

    launch_guard.check("run-1", "worker")

    assert resolutions == []


def test_check_invalid_settings_reports_settings_repair_without_retry(monkeypatch):
    resolutions = [_posture(True, launch_guard.settings_service.GATE_SOURCE_INVALID_SETTINGS)]
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: resolutions.pop(0),
    )
    monkeypatch.setattr(
        launch_guard.workflow_journal,
        "get_run",
        lambda run_id: pytest.fail("invalid settings must stop before journal reads"),
    )

    with pytest.raises(launch_guard.PlanInputsChangedError) as exc:
        launch_guard.check("run-1", "worker")

    assert "approval setting could not be read" in str(exc.value)
    assert "repair settings.json and then resume" in str(exc.value)
    assert "changed" not in str(exc.value)
    assert resolutions == []


@pytest.mark.parametrize(
    "guard",
    [
        None,
        {"profiles": [], "memory_enabled": False},
        {"profiles": {"worker": "sha256:frozen"}},
    ],
)
def test_check_rejects_corrupt_launch_guards_when_approval_is_on(monkeypatch, guard):
    row = _script_row(json.dumps({"launch_guard": guard}))
    monkeypatch.setattr(
        launch_guard.settings_service,
        "resolve_workflow_approval_posture",
        lambda: _posture(True),
    )
    monkeypatch.setattr(launch_guard.workflow_journal, "get_run", lambda run_id: row)

    with pytest.raises(launch_guard.PlanInputsChangedError, match="unreadable"):
        launch_guard.check("run-1", "worker")
