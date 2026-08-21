"""Unit tests for SupervisorDashboardStream.

Covers: delta-before-snapshot no-op, failed-patch drop, provider rollup over
mixed fleets, waiting-terminal surfacing, hierarchy construction, seen_set_dedup.
"""

from __future__ import annotations

import copy

import pytest

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.supervisor_dashboard import (
    SupervisorDashboardStream,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_RUN_FINISHED,
    AGUI_RUN_STARTED,
    AGUI_STATE_DELTA,
    AGUI_STATE_SNAPSHOT,
    AGUI_STEP_FINISHED,
    AGUI_STEP_STARTED,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_START,
)


def _emitter() -> RecordingUiEmitter:
    return RecordingUiEmitter()


def _fleet_snapshot() -> dict:
    """A minimal fleet snapshot matching build_dashboard_snapshot output shape."""
    return {
        "sessions": [
            {"id": "s1", "name": "dev-session", "status": "active"},
            {"id": "s2", "name": "test-session", "status": "terminated"},
        ],
        "terminals": [
            {
                "id": "t1",
                "session_name": "dev-session",
                "provider": "kiro_cli",
                "agent_profile": None,
                "window": "main",
                "status": "running",
                "last_active": "2026-07-04T00:00:00Z",
            },
            {
                "id": "t2",
                "session_name": "dev-session",
                "provider": "claude_code",
                "agent_profile": None,
                "window": "worker",
                "status": "waiting_user_answer",
                "last_active": "2026-07-04T00:01:00Z",
            },
            {
                "id": "t3",
                "session_name": "test-session",
                "provider": "codex",
                "agent_profile": None,
                "window": "test",
                "status": "idle",
                "last_active": "2026-07-04T00:02:00Z",
            },
        ],
        "counts": {"sessions": 2, "terminals": 3},
        "scopes": ["read", "write"],
    }


class TestDeltaBeforeSnapshot:
    """STATE_DELTA before any snapshot is a no-op, not an error."""

    def test_delta_before_snapshot_is_noop(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        # Feed a delta before any snapshot.
        dashboard.handle_frame(
            AGUI_STATE_DELTA,
            {"delta": [{"op": "replace", "path": "/counts/sessions", "value": 5}]},
            event_id="delta-1",
        )
        # Fleet should still be None, hierarchy empty.
        assert dashboard.hierarchy() == {}
        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["active_sessions"] == 0
        assert snapshot["counts"] == {"sessions": 0, "terminals": 0}


class TestFailedPatchDrop:
    """Failed patch application drops the delta without raising."""

    def test_invalid_patch_drops_silently(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        # Feed snapshot first.
        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        # Feed an invalid delta (path does not exist).
        dashboard.handle_frame(
            AGUI_STATE_DELTA,
            {"delta": [{"op": "replace", "path": "/nonexistent/deep/path", "value": 42}]},
            event_id="bad-delta",
        )

        # State should remain unchanged (the bad delta was dropped).
        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["counts"] == {"sessions": 2, "terminals": 3}

    def test_valid_delta_is_applied(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        # Valid delta: add a new terminal.
        new_terminal = {
            "id": "t4",
            "session_name": "dev-session",
            "provider": "mock_cli",
            "agent_profile": None,
            "window": "new",
            "status": "running",
            "last_active": None,
        }
        dashboard.handle_frame(
            AGUI_STATE_DELTA,
            {
                "delta": [
                    {"op": "add", "path": "/terminals/-", "value": new_terminal},
                    {"op": "replace", "path": "/counts/terminals", "value": 4},
                ]
            },
            event_id="good-delta",
        )

        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["counts"]["terminals"] == 4
        assert "mock_cli" in snapshot["by_provider"]


class TestProviderRollup:
    """Provider rollup over mixed fleets."""

    def test_counts_all_providers(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["by_provider"] == {
            "kiro_cli": 1,
            "claude_code": 1,
            "codex": 1,
        }

    def test_multiple_terminals_same_provider(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()
        # Add another kiro_cli terminal.
        fleet["terminals"].append(
            {
                "id": "t4",
                "session_name": "dev-session",
                "provider": "kiro_cli",
                "agent_profile": None,
                "window": "extra",
                "status": "running",
                "last_active": None,
            }
        )
        fleet["counts"]["terminals"] = 4

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["by_provider"]["kiro_cli"] == 2


class TestWaitingTerminals:
    """Waiting-terminal surfacing."""

    def test_surfaces_waiting_terminals(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["waiting_terminals"] == ["t2"]

    def test_no_waiting_terminals(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()
        # Remove waiting status.
        fleet["terminals"][1]["status"] = "running"

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["waiting_terminals"] == []


class TestHierarchy:
    """Hierarchy construction from fleet state."""

    def test_groups_terminals_by_session(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        h = dashboard.hierarchy()
        assert "dev-session" in h
        assert "test-session" in h
        assert set(h["dev-session"]["terminal_ids"]) == {"t1", "t2"}
        assert h["dev-session"]["terminal_count"] == 2
        assert h["dev-session"]["status"] == "active"
        assert h["test-session"]["terminal_ids"] == ["t3"]
        assert h["test-session"]["terminal_count"] == 1
        assert h["test-session"]["status"] == "terminated"

    def test_empty_fleet_returns_empty_hierarchy(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        assert dashboard.hierarchy() == {}


class TestSeenSetDedup:
    """Seen_Set_Dedup on id-bearing frames; state frames always folded."""

    def test_duplicate_event_id_skipped(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        # Feed two STEP_STARTED with the same event_id.
        dashboard.handle_frame(AGUI_STEP_STARTED, {"timestamp": "t1"}, event_id="step-1")
        dashboard.handle_frame(AGUI_STEP_STARTED, {"timestamp": "t2"}, event_id="step-1")

        # Only one should have been counted.
        assert dashboard._rollup[AGUI_STEP_STARTED] == 1

    def test_state_frames_always_folded(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet1 = _fleet_snapshot()
        fleet2 = copy.deepcopy(fleet1)
        fleet2["sessions"][0]["status"] = "closed"

        # Feed two snapshots without event_id (both should be processed).
        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet1}, event_id=None)
        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet2}, event_id=None)

        # Second snapshot should have replaced the first.
        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["active_sessions"] == 0  # both "closed" and "terminated"


class TestRollupCounters:
    """Rollup counters track lifecycle events."""

    def test_counts_lifecycle_events(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        dashboard.handle_frame(AGUI_RUN_STARTED, {}, event_id="run-1")
        dashboard.handle_frame(AGUI_STEP_STARTED, {}, event_id="step-1")
        dashboard.handle_frame(AGUI_STEP_FINISHED, {}, event_id="step-2")
        dashboard.handle_frame(AGUI_RUN_FINISHED, {}, event_id="run-2")
        dashboard.handle_frame(AGUI_TOOL_CALL_START, {}, event_id="tc-1")
        dashboard.handle_frame(AGUI_TOOL_CALL_END, {}, event_id="tc-2")

        assert dashboard._rollup[AGUI_RUN_STARTED] == 1
        assert dashboard._rollup[AGUI_STEP_STARTED] == 1
        assert dashboard._rollup[AGUI_STEP_FINISHED] == 1
        assert dashboard._rollup[AGUI_RUN_FINISHED] == 1
        assert dashboard._rollup[AGUI_TOOL_CALL_START] == 1
        assert dashboard._rollup[AGUI_TOOL_CALL_END] == 1


class TestLastActivity:
    """Last activity tracking."""

    def test_tracks_most_recent_event(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        dashboard.handle_frame(
            AGUI_STEP_STARTED, {"timestamp": "2026-07-04T00:05:00Z"}, event_id="e1"
        )
        dashboard.handle_frame(
            AGUI_STEP_FINISHED, {"timestamp": "2026-07-04T00:06:00Z"}, event_id="e2"
        )

        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["last_activity"]["event_id"] == "e2"

    def test_no_activity_before_events(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        snapshot = dashboard.supervisor_snapshot()
        assert snapshot["last_activity"] is None


class TestProjection:
    """projection() returns supervisor_snapshot."""

    def test_projection_matches_supervisor_snapshot(self) -> None:
        dashboard = SupervisorDashboardStream(_emitter())
        fleet = _fleet_snapshot()

        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": fleet}, event_id=None)

        assert dashboard.projection() == dashboard.supervisor_snapshot()
