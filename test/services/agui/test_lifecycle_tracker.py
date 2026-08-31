"""Unit tests for ToolCallLifecycleTracker.

Covers: orchestration-type discrimination, session-end closure, orphan
suppression, eviction, metadata-only assertions, byte-identical non-orchestration
mappings, and replay determinism.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services.agui.lifecycle_tracker import (
    ToolCallLifecycleTracker,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STEP_FINISHED,
    AGUI_STEP_STARTED,
    AGUI_TEXT_MESSAGE_CONTENT,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TOOL_CALL_START,
    to_agui_event,
)


def _record(kind: str, *, terminal_id=None, session_name=None, detail=None, rid="evt-1") -> dict:
    """Build an upstream-shaped normalized event record."""
    return {
        "id": rid,
        "kind": kind,
        "terminal_id": terminal_id,
        "session_name": session_name,
        "timestamp": "2026-07-04T00:00:00+00:00",
        "detail": detail or {},
    }


class TestOrchestrationTypeDiscrimination:
    """Handoff with orchestration_type=handoff/assign -> TOOL_CALL_START;
    send_message/absent -> TEXT_MESSAGE_CONTENT."""

    def test_handoff_type_handoff_becomes_tool_call_start(self) -> None:
        record = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
        )
        agui_type, data = to_agui_event(record)
        assert agui_type == AGUI_TOOL_CALL_START
        assert data["tool_call_name"] == "handoff"
        assert data["metadata"]["receiver"] == "r1"

    def test_handoff_type_assign_becomes_tool_call_start(self) -> None:
        record = _record(
            "handoff",
            terminal_id="r2",
            detail={"sender": "s", "receiver": "r2", "orchestration_type": "assign"},
        )
        agui_type, data = to_agui_event(record)
        assert agui_type == AGUI_TOOL_CALL_START
        assert data["tool_call_name"] == "assign"

    def test_handoff_type_send_message_becomes_text_message(self) -> None:
        record = _record(
            "handoff",
            terminal_id="r3",
            detail={"sender": "s", "receiver": "r3", "orchestration_type": "send_message"},
        )
        agui_type, data = to_agui_event(record)
        assert agui_type == AGUI_TEXT_MESSAGE_CONTENT
        assert data["delta"] == ""

    def test_handoff_absent_orchestration_type_becomes_text_message(self) -> None:
        record = _record(
            "handoff",
            terminal_id="r4",
            detail={"sender": "s", "receiver": "r4"},
        )
        agui_type, data = to_agui_event(record)
        assert agui_type == AGUI_TEXT_MESSAGE_CONTENT


class TestTrackerClosesOnCompletion:
    """ToolCallLifecycleTracker synthesizes TOOL_CALL_END when a receiver completes."""

    def test_tool_call_end_synthesized_on_step_finished(self) -> None:
        tracker = ToolCallLifecycleTracker()

        # Open a tool call via handoff with orchestration_type=handoff
        open_record = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-1",
        )
        open_frame = to_agui_event(open_record)
        frames = tracker.feed(open_record, open_frame)
        assert len(frames) == 1
        assert frames[0][0] == AGUI_TOOL_CALL_START

        # Complete the receiver terminal
        close_record = _record(
            "completion",
            terminal_id="r1",
            detail={"event_type": "post_kill_terminal", "agent_name": "dev"},
            rid="close-1",
        )
        close_frame = to_agui_event(close_record)
        frames = tracker.feed(close_record, close_frame)

        # Should have: STEP_FINISHED + TOOL_CALL_END
        types = [f[0] for f in frames]
        assert AGUI_STEP_FINISHED in types
        assert AGUI_TOOL_CALL_END in types
        end_frame = next(f for f in frames if f[0] == AGUI_TOOL_CALL_END)
        assert end_frame[1]["tool_call_id"] == "open-1"
        assert end_frame[1]["tool_call_name"] == "handoff"

    def test_a2a_delegation_produces_result_and_end(self) -> None:
        tracker = ToolCallLifecycleTracker()

        # Open a tool call via a2a_delegation
        open_record = _record(
            "a2a_delegation",
            terminal_id="t1",
            detail={"sender": "s", "receiver": "r-a2a", "orchestration_type": "a2a_send"},
            rid="a2a-1",
        )
        open_frame = to_agui_event(open_record)
        frames = tracker.feed(open_record, open_frame)
        assert frames[0][0] == AGUI_TOOL_CALL_START

        # Complete the receiver
        close_record = _record(
            "completion",
            terminal_id="r-a2a",
            detail={"event_type": "post_kill_terminal", "agent_name": "worker"},
            rid="close-a2a",
        )
        close_frame = to_agui_event(close_record)
        frames = tracker.feed(close_record, close_frame)

        types = [f[0] for f in frames]
        assert AGUI_TOOL_CALL_RESULT in types
        assert AGUI_TOOL_CALL_END in types
        # RESULT comes before END
        assert types.index(AGUI_TOOL_CALL_RESULT) < types.index(AGUI_TOOL_CALL_END)


class TestOrphanSuppression:
    """No orphan TOOL_CALL_END for unknown receivers."""

    def test_completion_without_open_produces_no_closer(self) -> None:
        tracker = ToolCallLifecycleTracker()

        # A terminal completes that was never opened as a tool call
        close_record = _record(
            "completion",
            terminal_id="unknown-terminal",
            detail={"event_type": "post_kill_terminal", "agent_name": "dev"},
            rid="orphan-1",
        )
        close_frame = to_agui_event(close_record)
        frames = tracker.feed(close_record, close_frame)

        # Only the original frame, no synthesized closers
        assert len(frames) == 1
        assert frames[0][0] == AGUI_STEP_FINISHED

    def test_double_completion_produces_only_one_closer(self) -> None:
        tracker = ToolCallLifecycleTracker()

        # Open
        open_record = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "assign"},
            rid="open-1",
        )
        tracker.feed(open_record, to_agui_event(open_record))

        # First completion
        close1 = _record(
            "completion",
            terminal_id="r1",
            detail={"event_type": "post_kill_terminal"},
            rid="close-1",
        )
        frames1 = tracker.feed(close1, to_agui_event(close1))
        assert AGUI_TOOL_CALL_END in [f[0] for f in frames1]

        # Second completion (same terminal) - no closer
        close2 = _record(
            "completion",
            terminal_id="r1",
            detail={"event_type": "post_kill_terminal"},
            rid="close-2",
        )
        frames2 = tracker.feed(close2, to_agui_event(close2))
        assert AGUI_TOOL_CALL_END not in [f[0] for f in frames2]


class TestSessionEndClosure:
    """close_all() synthesizes closers for all remaining open calls."""

    def test_close_all_produces_end_for_each_open(self) -> None:
        tracker = ToolCallLifecycleTracker()

        # Open two tool calls
        for i in range(2):
            record = _record(
                "handoff",
                terminal_id=f"r{i}",
                detail={"sender": "s", "receiver": f"r{i}", "orchestration_type": "handoff"},
                rid=f"open-{i}",
            )
            tracker.feed(record, to_agui_event(record))

        assert tracker.open_count == 2
        frames = tracker.close_all()
        end_frames = [f for f in frames if f[0] == AGUI_TOOL_CALL_END]
        assert len(end_frames) == 2
        assert tracker.open_count == 0

    def test_close_all_empty_when_nothing_open(self) -> None:
        tracker = ToolCallLifecycleTracker()
        assert tracker.close_all() == []


class TestEviction:
    """Bounded map with oldest-first eviction."""

    def test_eviction_at_capacity(self) -> None:
        tracker = ToolCallLifecycleTracker(max_open=3)

        # Open 4 tool calls (cap is 3, so oldest should be evicted)
        for i in range(4):
            record = _record(
                "handoff",
                terminal_id=f"r{i}",
                detail={"sender": "s", "receiver": f"r{i}", "orchestration_type": "handoff"},
                rid=f"open-{i}",
            )
            tracker.feed(record, to_agui_event(record))

        assert tracker.open_count == 3

        # Completing receiver "r0" should NOT produce a closer (evicted)
        close_r0 = _record(
            "completion",
            terminal_id="r0",
            detail={"event_type": "post_kill_terminal"},
            rid="close-r0",
        )
        frames = tracker.feed(close_r0, to_agui_event(close_r0))
        assert AGUI_TOOL_CALL_END not in [f[0] for f in frames]

        # Completing receiver "r3" should produce a closer (newest, still tracked)
        close_r3 = _record(
            "completion",
            terminal_id="r3",
            detail={"event_type": "post_kill_terminal"},
            rid="close-r3",
        )
        frames = tracker.feed(close_r3, to_agui_event(close_r3))
        assert AGUI_TOOL_CALL_END in [f[0] for f in frames]


class TestMetadataOnly:
    """Closer frames carry metadata but no message bodies."""

    def test_tool_call_end_has_no_body_content(self) -> None:
        tracker = ToolCallLifecycleTracker()
        open_record = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-1",
        )
        tracker.feed(open_record, to_agui_event(open_record))

        close_record = _record(
            "completion",
            terminal_id="r1",
            detail={"event_type": "post_kill_terminal"},
            rid="close-1",
        )
        frames = tracker.feed(close_record, to_agui_event(close_record))
        end_frame = next(f for f in frames if f[0] == AGUI_TOOL_CALL_END)
        data = end_frame[1]
        # No body, delta, content, or message fields
        assert "delta" not in data
        assert "content" not in data
        assert "message" not in data
        assert "body" not in data


class TestNonOrchestrationPassthrough:
    """Non-orchestration frames pass through byte-identical."""

    def test_launch_frame_unchanged(self) -> None:
        tracker = ToolCallLifecycleTracker()
        record = _record(
            "launch",
            terminal_id="t1",
            detail={"agent_name": "dev", "provider": "mock_cli"},
        )
        mapped = to_agui_event(record)
        frames = tracker.feed(record, mapped)
        assert len(frames) == 1
        assert frames[0] == mapped

    def test_text_message_frame_unchanged(self) -> None:
        tracker = ToolCallLifecycleTracker()
        record = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "send_message"},
        )
        mapped = to_agui_event(record)
        frames = tracker.feed(record, mapped)
        assert len(frames) == 1
        assert frames[0] == mapped


class TestReplayDeterminism:
    """Same input records always produce same output frames."""

    def test_deterministic_output_on_replay(self) -> None:
        records = [
            _record(
                "handoff",
                terminal_id="r1",
                detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
                rid="open-1",
            ),
            _record(
                "launch",
                terminal_id="r1",
                detail={"agent_name": "dev", "provider": "mock_cli"},
                rid="launch-1",
            ),
            _record(
                "completion",
                terminal_id="r1",
                detail={"event_type": "post_kill_terminal", "agent_name": "dev"},
                rid="close-1",
            ),
        ]

        def run_through():
            tracker = ToolCallLifecycleTracker()
            all_frames = []
            for rec in records:
                mapped = to_agui_event(rec)
                frames = tracker.feed(rec, mapped)
                all_frames.extend(frames)
            all_frames.extend(tracker.close_all())
            return all_frames

        run1 = run_through()
        run2 = run_through()
        assert run1 == run2


class TestSameReceiverCollision:
    """When two TOOL_CALL_START frames target the same receiver before the first
    completes, the tracker synthesizes a TOOL_CALL_END for the first (disposition
    'superseded') before registering the second."""

    def test_second_handoff_supersedes_first(self) -> None:
        tracker = ToolCallLifecycleTracker()

        # Open first tool call targeting receiver "r1"
        open1 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-1",
        )
        frames1 = tracker.feed(open1, to_agui_event(open1))
        assert len(frames1) == 1
        assert frames1[0][0] == AGUI_TOOL_CALL_START
        assert tracker.open_count == 1

        # Open second tool call targeting the same receiver "r1"
        open2 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s2", "receiver": "r1", "orchestration_type": "assign"},
            rid="open-2",
        )
        frames2 = tracker.feed(open2, to_agui_event(open2))

        # Expect: TOOL_CALL_END(superseded) for first, then TOOL_CALL_START for second
        types = [f[0] for f in frames2]
        assert AGUI_TOOL_CALL_END in types
        assert AGUI_TOOL_CALL_START in types

        # The superseded closer comes first
        end_idx = types.index(AGUI_TOOL_CALL_END)
        start_idx = types.index(AGUI_TOOL_CALL_START)
        assert end_idx < start_idx

        # The superseded TOOL_CALL_END has the first call's id and disposition
        end_frame = frames2[end_idx]
        assert end_frame[1]["tool_call_id"] == "open-1"
        assert end_frame[1]["metadata"]["disposition"] == "superseded"

        # Only one open call remains
        assert tracker.open_count == 1

    def test_superseded_end_has_correct_tool_call_name(self) -> None:
        tracker = ToolCallLifecycleTracker()

        open1 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-1",
        )
        tracker.feed(open1, to_agui_event(open1))

        open2 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s2", "receiver": "r1", "orchestration_type": "assign"},
            rid="open-2",
        )
        frames2 = tracker.feed(open2, to_agui_event(open2))

        end_frame = next(f for f in frames2 if f[0] == AGUI_TOOL_CALL_END)
        assert end_frame[1]["tool_call_name"] == "handoff"

    def test_completion_after_superseded_only_closes_latest(self) -> None:
        """After superseding, completing the receiver closes only the new call."""
        tracker = ToolCallLifecycleTracker()

        open1 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-1",
        )
        tracker.feed(open1, to_agui_event(open1))

        open2 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s2", "receiver": "r1", "orchestration_type": "assign"},
            rid="open-2",
        )
        tracker.feed(open2, to_agui_event(open2))

        # Now complete receiver "r1"
        close = _record(
            "completion",
            terminal_id="r1",
            detail={"event_type": "post_kill_terminal", "agent_name": "dev"},
            rid="close-1",
        )
        frames = tracker.feed(close, to_agui_event(close))

        end_frames = [f for f in frames if f[0] == AGUI_TOOL_CALL_END]
        assert len(end_frames) == 1
        # Should close the second (latest) open call
        assert end_frames[0][1]["tool_call_id"] == "open-2"
        assert end_frames[0][1]["tool_call_name"] == "assign"
        # No disposition on normal completion
        assert "disposition" not in end_frames[0][1]["metadata"]

        # No open calls remain
        assert tracker.open_count == 0

    def test_a2a_delegation_superseded_produces_result_and_end(self) -> None:
        """Superseding an a2a_delegation open also emits TOOL_CALL_RESULT."""
        tracker = ToolCallLifecycleTracker()

        open1 = _record(
            "a2a_delegation",
            terminal_id="t1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "a2a_send"},
            rid="a2a-1",
        )
        tracker.feed(open1, to_agui_event(open1))

        open2 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s2", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-2",
        )
        frames = tracker.feed(open2, to_agui_event(open2))

        types = [f[0] for f in frames]
        assert AGUI_TOOL_CALL_RESULT in types
        assert AGUI_TOOL_CALL_END in types
        assert AGUI_TOOL_CALL_START in types

        # RESULT and END for superseded come before the new START
        result_idx = types.index(AGUI_TOOL_CALL_RESULT)
        end_idx = types.index(AGUI_TOOL_CALL_END)
        start_idx = types.index(AGUI_TOOL_CALL_START)
        assert result_idx < end_idx < start_idx

        # Check disposition on the superseded frames
        end_frame = frames[end_idx]
        assert end_frame[1]["metadata"]["disposition"] == "superseded"
        result_frame = frames[result_idx]
        assert result_frame[1]["metadata"]["disposition"] == "superseded"

    def test_triple_handoff_to_same_receiver(self) -> None:
        """Three consecutive handoffs to the same receiver: each supersedes the previous."""
        tracker = ToolCallLifecycleTracker()

        # First open
        open1 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s1", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-1",
        )
        frames1 = tracker.feed(open1, to_agui_event(open1))
        assert tracker.open_count == 1

        # Second open (supersedes first)
        open2 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s2", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-2",
        )
        frames2 = tracker.feed(open2, to_agui_event(open2))
        superseded_ends_2 = [f for f in frames2 if f[0] == AGUI_TOOL_CALL_END]
        assert len(superseded_ends_2) == 1
        assert superseded_ends_2[0][1]["tool_call_id"] == "open-1"
        assert tracker.open_count == 1

        # Third open (supersedes second)
        open3 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s3", "receiver": "r1", "orchestration_type": "handoff"},
            rid="open-3",
        )
        frames3 = tracker.feed(open3, to_agui_event(open3))
        superseded_ends_3 = [f for f in frames3 if f[0] == AGUI_TOOL_CALL_END]
        assert len(superseded_ends_3) == 1
        assert superseded_ends_3[0][1]["tool_call_id"] == "open-2"
        assert tracker.open_count == 1

        # Final completion closes the third
        close = _record(
            "completion",
            terminal_id="r1",
            detail={"event_type": "post_kill_terminal"},
            rid="close-1",
        )
        close_frames = tracker.feed(close, to_agui_event(close))
        end_frames = [f for f in close_frames if f[0] == AGUI_TOOL_CALL_END]
        assert len(end_frames) == 1
        assert end_frames[0][1]["tool_call_id"] == "open-3"
        assert tracker.open_count == 0


class TestCloseDisposition:
    """TOOL_CALL_END carries a ``closed_by`` disposition so clients can tell a
    real completion from a lifecycle-forced close (R6.3)."""

    def _open(self, tracker: ToolCallLifecycleTracker, receiver: str) -> None:
        rec = _record(
            "handoff",
            terminal_id=receiver,
            detail={"sender": "s", "receiver": receiver, "orchestration_type": "handoff"},
        )
        tracker.feed(rec, to_agui_event(rec))

    def _ends(self, frames):
        return [d for (ft, d) in frames if ft == AGUI_TOOL_CALL_END]

    def test_completion_close_tagged_completion(self) -> None:
        tracker = ToolCallLifecycleTracker()
        self._open(tracker, "r1")
        done = _record("completion", terminal_id="r1")
        ends = self._ends(tracker.feed(done, to_agui_event(done)))
        assert ends and ends[0]["metadata"]["closed_by"] == "completion"

    def test_session_end_close_tagged_session_end(self) -> None:
        tracker = ToolCallLifecycleTracker()
        self._open(tracker, "r1")
        ends = self._ends(tracker.close_all())
        assert ends and ends[0]["metadata"]["closed_by"] == "session_end"

    def test_superseded_close_tagged_superseded(self) -> None:
        tracker = ToolCallLifecycleTracker()
        self._open(tracker, "r1")
        rec2 = _record(
            "handoff",
            terminal_id="r1",
            detail={"sender": "s", "receiver": "r1", "orchestration_type": "handoff"},
        )
        ends = self._ends(tracker.feed(rec2, to_agui_event(rec2)))
        assert ends and ends[0]["metadata"]["closed_by"] == "superseded"
        assert ends[0]["metadata"]["disposition"] == "superseded"
