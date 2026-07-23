"""Unit tests for CrossProviderStateSync.

Covers: snapshot replace, delta apply, converges_with true/false,
providers_seen across kiro_cli/claude_code/codex terminals,
delta-before-snapshot no-op, failed-patch drop, seen_set_dedup.
"""

from __future__ import annotations

import copy

import pytest

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.cross_provider_sync import (
    CrossProviderStateSync,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STATE_DELTA,
    AGUI_STATE_SNAPSHOT,
)


def _emitter() -> RecordingUiEmitter:
    return RecordingUiEmitter()


def _multi_provider_state() -> dict:
    """State with terminals from multiple providers."""
    return {
        "sessions": [
            {"id": "s1", "name": "main", "status": "active"},
        ],
        "terminals": [
            {"id": "t1", "session_name": "main", "provider": "kiro_cli", "status": "running"},
            {"id": "t2", "session_name": "main", "provider": "claude_code", "status": "idle"},
            {"id": "t3", "session_name": "main", "provider": "codex", "status": "running"},
        ],
        "counts": {"sessions": 1, "terminals": 3},
        "scopes": [],
    }


class TestSnapshotReplace:
    """STATE_SNAPSHOT deep-copy replaces internal state."""

    def test_snapshot_sets_state(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        assert sync.shared_state() == state

    def test_second_snapshot_replaces_first(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state1 = _multi_provider_state()
        state2 = copy.deepcopy(state1)
        state2["sessions"][0]["status"] = "closed"

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state1}, event_id=None)
        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state2}, event_id=None)

        assert sync.shared_state()["sessions"][0]["status"] == "closed"

    def test_snapshot_is_deep_copy(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        # Mutate original - should not affect internal state.
        state["sessions"][0]["status"] = "mutated"
        assert sync.shared_state()["sessions"][0]["status"] == "active"


class TestDeltaApply:
    """STATE_DELTA applies patch via apply_json_patch_strict."""

    def test_valid_delta_applied(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        sync.handle_frame(
            AGUI_STATE_DELTA,
            {
                "delta": [
                    {"op": "replace", "path": "/counts/terminals", "value": 4},
                ]
            },
            event_id="d-1",
        )

        assert sync.shared_state()["counts"]["terminals"] == 4

    def test_delta_before_snapshot_is_noop(self) -> None:
        sync = CrossProviderStateSync(_emitter())

        sync.handle_frame(
            AGUI_STATE_DELTA,
            {"delta": [{"op": "replace", "path": "/counts/terminals", "value": 99}]},
            event_id="d-1",
        )

        assert sync.shared_state() is None

    def test_failed_delta_drops_silently(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        # Invalid path.
        sync.handle_frame(
            AGUI_STATE_DELTA,
            {"delta": [{"op": "replace", "path": "/no/such/path", "value": 1}]},
            event_id="d-bad",
        )

        # State unchanged.
        assert sync.shared_state()["counts"]["terminals"] == 3


class TestConvergesWith:
    """converges_with returns true when state matches authoritative."""

    def test_converges_when_equal(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        authoritative = copy.deepcopy(state)
        assert sync.converges_with(authoritative) is True

    def test_does_not_converge_when_different(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        authoritative = copy.deepcopy(state)
        authoritative["counts"]["terminals"] = 99
        assert sync.converges_with(authoritative) is False

    def test_does_not_converge_when_no_snapshot(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        assert sync.converges_with({"anything": True}) is False

    def test_converges_after_delta_applied(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        # Apply a delta.
        sync.handle_frame(
            AGUI_STATE_DELTA,
            {"delta": [{"op": "replace", "path": "/counts/terminals", "value": 5}]},
            event_id="d-1",
        )

        # Build expected authoritative state.
        authoritative = copy.deepcopy(state)
        authoritative["counts"]["terminals"] = 5
        assert sync.converges_with(authoritative) is True


class TestProvidersSeen:
    """providers_seen extracts unique provider values from terminals."""

    def test_all_providers_extracted(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        providers = sync.providers_seen()
        assert providers == ["claude_code", "codex", "kiro_cli"]

    def test_empty_when_no_snapshot(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        assert sync.providers_seen() == []

    def test_empty_providers_filtered(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = {
            "sessions": [],
            "terminals": [
                {"id": "t1", "provider": "kiro_cli"},
                {"id": "t2", "provider": ""},
                {"id": "t3"},
            ],
            "counts": {"sessions": 0, "terminals": 3},
            "scopes": [],
        }

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        assert sync.providers_seen() == ["kiro_cli"]

    def test_duplicate_providers_deduplicated(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = {
            "sessions": [],
            "terminals": [
                {"id": "t1", "provider": "kiro_cli"},
                {"id": "t2", "provider": "kiro_cli"},
                {"id": "t3", "provider": "claude_code"},
            ],
            "counts": {"sessions": 0, "terminals": 3},
            "scopes": [],
        }

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        assert sync.providers_seen() == ["claude_code", "kiro_cli"]


class TestSeenSetDedup:
    """Seen_Set_Dedup on id-bearing frames; state frames always folded."""

    def test_duplicate_event_id_skipped(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        # Same event_id delta should be skipped on replay.
        sync.handle_frame(
            AGUI_STATE_DELTA,
            {"delta": [{"op": "replace", "path": "/counts/terminals", "value": 10}]},
            event_id="d-1",
        )
        sync.handle_frame(
            AGUI_STATE_DELTA,
            {"delta": [{"op": "replace", "path": "/counts/terminals", "value": 20}]},
            event_id="d-1",
        )

        # Only the first delta should be applied.
        assert sync.shared_state()["counts"]["terminals"] == 10

    def test_state_frames_without_id_always_folded(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state1 = _multi_provider_state()
        state2 = copy.deepcopy(state1)
        state2["counts"]["terminals"] = 99

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state1}, event_id=None)
        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state2}, event_id=None)

        assert sync.shared_state()["counts"]["terminals"] == 99


class TestProjection:
    """projection() returns shared_state or empty dict."""

    def test_projection_before_snapshot(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        assert sync.projection() == {}

    def test_projection_after_snapshot(self) -> None:
        sync = CrossProviderStateSync(_emitter())
        state = _multi_provider_state()

        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": state}, event_id=None)

        assert sync.projection() == state
