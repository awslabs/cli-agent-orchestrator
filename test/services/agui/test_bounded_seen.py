"""Unit tests for the bounded seen-set (P0-1 / P1-5 / P1-6).

Covers: BoundedSeen cap enforcement + oldest-half eviction, recent-id dedup
correctness after eviction, and that the constructs' ``_seen`` / ``_started_ids``
sets stay bounded when folding more than ``_SEEN_CAP`` id-bearing frames — the
memory-leak failure mode the unbounded ``set`` had on a long-lived stream.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services.agui.base import (
    _SEEN_CAP,
    BoundedSeen,
    RecordingUiEmitter,
)
from cli_agent_orchestrator.services.agui.session_timeline import (
    MultiAgentSessionTimeline,
)
from cli_agent_orchestrator.services.agui.supervisor_dashboard import (
    SupervisorDashboardStream,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STEP_STARTED,
    AGUI_TOOL_CALL_START,
)


def test_bounded_seen_rejects_tiny_cap() -> None:
    with pytest.raises(ValueError):
        BoundedSeen(cap=1)


def test_bounded_seen_enforces_cap_under_load() -> None:
    seen = BoundedSeen(cap=100)
    for i in range(1000):
        seen.add(f"evt-{i}")
    # Never grows past the cap.
    assert len(seen) <= 100


def test_bounded_seen_add_is_idempotent() -> None:
    seen = BoundedSeen(cap=100)
    seen.add("dup")
    seen.add("dup")
    assert len(seen) == 1
    assert "dup" in seen


def test_bounded_seen_dedups_recent_ids_after_eviction() -> None:
    cap = 100
    seen = BoundedSeen(cap=cap)
    for i in range(1000):
        seen.add(f"evt-{i}")
    # The most recently added ids (well within the retained half) still dedup.
    assert "evt-999" in seen
    assert "evt-950" in seen
    # The oldest ids were evicted (bounded memory).
    assert "evt-0" not in seen


def test_bounded_seen_retains_just_added_item_on_eviction_boundary() -> None:
    cap = 10
    seen = BoundedSeen(cap=cap)
    for i in range(cap + 1):  # trip exactly one eviction
        seen.add(f"e{i}")
    # The item that tripped the eviction is the newest and must survive.
    assert f"e{cap}" in seen
    assert len(seen) <= cap


def test_supervisor_dashboard_seen_is_bounded() -> None:
    d = SupervisorDashboardStream(RecordingUiEmitter())
    n = _SEEN_CAP + 5000
    for i in range(n):
        d.handle_frame(AGUI_STEP_STARTED, {"timestamp": i}, event_id=f"evt-{i}")
    assert len(d._seen) <= _SEEN_CAP
    # Recent id still deduped: re-folding it does not change bounded size.
    before = len(d._seen)
    d.handle_frame(AGUI_STEP_STARTED, {"timestamp": n - 1}, event_id=f"evt-{n - 1}")
    assert len(d._seen) == before


def test_session_timeline_seen_and_started_ids_are_bounded() -> None:
    t = MultiAgentSessionTimeline(RecordingUiEmitter())
    n = _SEEN_CAP + 5000
    for i in range(n):
        # Distinct event_id (bounds _seen) and distinct tool_call_id (bounds
        # _started_ids via the duplicate-start guard).
        t.handle_frame(
            AGUI_TOOL_CALL_START,
            {"tool_call_id": f"tc-{i}", "tool_call_name": "handoff"},
            event_id=f"evt-{i}",
        )
    assert len(t._seen) <= _SEEN_CAP
    assert len(t._started_ids) <= _SEEN_CAP
