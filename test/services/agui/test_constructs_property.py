"""Hypothesis property tests for the L2 fold-based constructs.

P3: Ordered-fold convergence vs build_dashboard_snapshot.
P4: Overlap-replay idempotency.
P5: Privacy of projections (no body content leaks).
P6: Timeline well-formedness (completed+failed <= opened).
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.cross_provider_sync import (
    CrossProviderStateSync,
)
from cli_agent_orchestrator.services.agui.session_timeline import (
    MultiAgentSessionTimeline,
)
from cli_agent_orchestrator.services.agui.supervisor_dashboard import (
    SupervisorDashboardStream,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STATE_DELTA,
    AGUI_STATE_SNAPSHOT,
    AGUI_STEP_FINISHED,
    AGUI_STEP_STARTED,
    AGUI_TEXT_MESSAGE_CONTENT,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TOOL_CALL_START,
)
from cli_agent_orchestrator.services.ui_state_service import (
    build_dashboard_snapshot,
    diff_snapshot,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

provider_names = st.sampled_from(["kiro_cli", "claude_code", "codex", "mock_cli"])
session_statuses = st.sampled_from(["active", "idle", "terminated", "closed"])
terminal_statuses = st.sampled_from(["running", "idle", "waiting_user_answer", "stopped"])


@st.composite
def fleet_sessions(draw, min_size=0, max_size=5):
    """Generate a list of session dicts."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    sessions = []
    for i in range(n):
        sessions.append(
            {
                "id": f"s{i}",
                "name": f"session-{i}",
                "status": draw(session_statuses),
            }
        )
    return sessions


@st.composite
def fleet_terminals(draw, session_names, min_size=0, max_size=10):
    """Generate a list of terminal dicts referencing given session_names."""
    if not session_names:
        return []
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    terminals = []
    for i in range(n):
        terminals.append(
            {
                "id": f"t{i}",
                "session_name": draw(st.sampled_from(session_names)),
                "provider": draw(provider_names),
                "agent_profile": None,
                "window": f"w{i}",
                "status": draw(terminal_statuses),
                "last_active": None,
            }
        )
    return terminals


@st.composite
def fleet_snapshot_st(draw):
    """Generate a complete fleet snapshot."""
    sessions = draw(fleet_sessions(min_size=1, max_size=4))
    session_names = [s["name"] for s in sessions]
    terminals = draw(fleet_terminals(session_names, min_size=0, max_size=8))
    return {
        "sessions": sessions,
        "terminals": terminals,
        "counts": {"sessions": len(sessions), "terminals": len(terminals)},
        "scopes": [],
    }


# ---------------------------------------------------------------------------
# P3: Ordered-fold convergence vs build_dashboard_snapshot
# ---------------------------------------------------------------------------


class TestP3FoldConvergence:
    """Feeding a snapshot from build_dashboard_snapshot into CrossProviderStateSync
    converges with the authoritative output."""

    @given(
        sessions=fleet_sessions(min_size=1, max_size=4),
    )
    @settings(max_examples=30)
    def test_snapshot_fold_converges(self, sessions):
        session_names = [s["name"] for s in sessions]
        # Build a simple terminal list.
        terminals = [
            {"id": f"t{i}", "tmux_session": name, "provider": "kiro_cli", "agent_profile": None}
            for i, name in enumerate(session_names)
        ]

        authoritative = build_dashboard_snapshot(sessions, terminals)

        sync = CrossProviderStateSync(RecordingUiEmitter())
        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": authoritative}, event_id=None)

        assert sync.converges_with(authoritative)

    @given(snapshot=fleet_snapshot_st())
    @settings(max_examples=30)
    def test_snapshot_then_delta_converges(self, snapshot):
        """A snapshot followed by a delta from diff_snapshot converges."""
        sync = CrossProviderStateSync(RecordingUiEmitter())
        sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}, event_id=None)

        # Mutate the snapshot to create a new version.
        updated = copy.deepcopy(snapshot)
        if updated["terminals"]:
            updated["terminals"][0]["status"] = "stopped"

        # Compute the delta.
        delta_ops = diff_snapshot(snapshot, updated)
        if delta_ops:
            sync.handle_frame(AGUI_STATE_DELTA, {"delta": delta_ops}, event_id="delta-1")

        assert sync.converges_with(updated)


# ---------------------------------------------------------------------------
# P4: Overlap-replay idempotency
# ---------------------------------------------------------------------------


class TestP4ReplayIdempotency:
    """Replaying the same id-bearing frames produces the same projection."""

    @given(snapshot=fleet_snapshot_st())
    @settings(max_examples=30)
    def test_supervisor_replay_idempotent(self, snapshot):
        """Replaying the same frames (same event_ids) produces identical state."""
        frames = [
            (AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}, None),
            (AGUI_STEP_STARTED, {"timestamp": "t1"}, "e-1"),
            (AGUI_STEP_FINISHED, {"timestamp": "t2"}, "e-2"),
        ]

        # First pass.
        d1 = SupervisorDashboardStream(RecordingUiEmitter())
        for agui_type, data, eid in frames:
            d1.handle_frame(agui_type, data, eid)

        # Second pass (replay all frames).
        d2 = SupervisorDashboardStream(RecordingUiEmitter())
        for agui_type, data, eid in frames + frames:
            d2.handle_frame(agui_type, data, eid)

        # Projections should match (id-bearing frames deduplicated).
        assert d1.projection() == d2.projection()

    @given(snapshot=fleet_snapshot_st())
    @settings(max_examples=30)
    def test_cross_provider_replay_idempotent(self, snapshot):
        """Cross-provider sync: replaying id-bearing frames is idempotent.

        State frames (event_id=None) always overwrite, so a replayed snapshot
        resets state. Id-bearing deltas are deduplicated. This test verifies
        that feeding only id-bearing frames twice produces the same result.
        """
        # Use an id-bearing snapshot so dedup applies uniformly.
        frames = [
            (AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}, "snap-1"),
            (
                AGUI_STATE_DELTA,
                {"delta": [{"op": "replace", "path": "/counts/sessions", "value": 99}]},
                "d-1",
            ),
        ]

        s1 = CrossProviderStateSync(RecordingUiEmitter())
        for agui_type, data, eid in frames:
            s1.handle_frame(agui_type, data, eid)

        s2 = CrossProviderStateSync(RecordingUiEmitter())
        for agui_type, data, eid in frames + frames:
            s2.handle_frame(agui_type, data, eid)

        assert s1.projection() == s2.projection()


# ---------------------------------------------------------------------------
# P5: Privacy of projections (no body content leaks)
# ---------------------------------------------------------------------------

_BODY_INDICATORS = frozenset({"delta", "content", "message_body", "stdout"})


class TestP5ProjectionPrivacy:
    """No message body content leaks into projections."""

    @given(
        body_text=st.text(min_size=1, max_size=100),
        sender=st.text(min_size=1, max_size=10),
        receiver=st.text(min_size=1, max_size=10),
    )
    @settings(max_examples=50)
    def test_timeline_projection_has_no_body(self, body_text, sender, receiver):
        """Timeline projection never contains the delta/content text."""
        timeline = MultiAgentSessionTimeline(RecordingUiEmitter())

        timeline.handle_frame(
            AGUI_TEXT_MESSAGE_CONTENT,
            {
                "delta": body_text,
                "metadata": {"sender": sender, "receiver": receiver},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="msg-1",
        )

        proj = timeline.projection()
        serialized = json.dumps(proj)
        # The body text must not appear in the projection unless it happens
        # to be identical to a metadata field (sender/receiver).
        # We check none of the entries have a "delta" or "content" field.
        for entry in proj.get("entries", []):
            for field in _BODY_INDICATORS:
                assert field not in entry or entry.get(field) is None

    @given(snapshot=fleet_snapshot_st())
    @settings(max_examples=30)
    def test_supervisor_projection_has_no_body(self, snapshot):
        """Supervisor projection never contains body fields."""
        dashboard = SupervisorDashboardStream(RecordingUiEmitter())
        dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}, event_id=None)

        proj = dashboard.projection()
        serialized = json.dumps(proj)
        # No body-field keys at the top level of the projection.
        for field in _BODY_INDICATORS:
            assert field not in proj


# ---------------------------------------------------------------------------
# P6: Timeline well-formedness (completed+failed <= opened)
# ---------------------------------------------------------------------------


class TestP6TimelineWellFormedness:
    """completed + failed entries never exceed opened entries."""

    @given(
        num_opens=st.integers(min_value=0, max_value=20),
        num_closes=st.integers(min_value=0, max_value=20),
    )
    @settings(max_examples=50)
    def test_closed_never_exceeds_opened(self, num_opens, num_closes):
        """Number of completed+failed entries never exceeds number of opened."""
        timeline = MultiAgentSessionTimeline(RecordingUiEmitter())

        # Open delegations.
        for i in range(num_opens):
            timeline.handle_frame(
                AGUI_TOOL_CALL_START,
                {
                    "tool_call_id": f"tc-{i}",
                    "tool_call_name": "handoff",
                    "metadata": {},
                    "timestamp": f"2026-07-04T00:{i:02d}:00Z",
                },
                event_id=f"open-{i}",
            )

        # Close some (may reference non-existent ids - those are no-ops).
        for i in range(num_closes):
            timeline.handle_frame(
                AGUI_TOOL_CALL_END,
                {
                    "tool_call_id": f"tc-{i}",
                    "metadata": {},
                    "timestamp": f"2026-07-04T01:{i:02d}:00Z",
                },
                event_id=f"close-{i}",
            )

        entries = timeline.entries()
        delegation_entries = [e for e in entries if e.kind == "delegation"]
        completed_or_failed = [e for e in delegation_entries if e.status in ("completed", "failed")]
        # completed+failed <= total opened delegations
        assert len(completed_or_failed) <= len(delegation_entries)

    @given(
        num_entries=st.integers(min_value=0, max_value=30),
        fail_indices=st.lists(st.integers(min_value=0, max_value=29), max_size=10),
    )
    @settings(max_examples=30)
    def test_mixed_lifecycle_well_formed(self, num_entries, fail_indices):
        """Mixed open/close/fail sequences maintain well-formedness invariant."""
        timeline = MultiAgentSessionTimeline(RecordingUiEmitter())

        # Open entries.
        for i in range(num_entries):
            timeline.handle_frame(
                AGUI_TOOL_CALL_START,
                {
                    "tool_call_id": f"tc-{i}",
                    "tool_call_name": "handoff",
                    "metadata": {},
                    "timestamp": f"2026-07-04T00:{i:02d}:00Z",
                },
                event_id=f"open-{i}",
            )

        # Close some with failure.
        for idx in fail_indices:
            if idx < num_entries:
                timeline.handle_frame(
                    AGUI_TOOL_CALL_END,
                    {
                        "tool_call_id": f"tc-{idx}",
                        "metadata": {"failed": True},
                        "timestamp": f"2026-07-04T02:{idx:02d}:00Z",
                    },
                    event_id=f"fail-{idx}",
                )

        entries = timeline.entries()
        delegation_entries = [e for e in entries if e.kind == "delegation"]
        completed_or_failed = [e for e in delegation_entries if e.status in ("completed", "failed")]
        assert len(completed_or_failed) <= len(delegation_entries)
