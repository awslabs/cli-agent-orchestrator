"""Unit tests for MultiAgentSessionTimeline.

Covers: ordering tiebreak, unknown-closer/duplicate-start no-ops, failure
disposition, cap eviction boundary, delta never stored, seen_set_dedup.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.session_timeline import (
    MultiAgentSessionTimeline,
    TimelineEntry,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_TEXT_MESSAGE_CONTENT,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TOOL_CALL_START,
)


def _emitter() -> RecordingUiEmitter:
    return RecordingUiEmitter()


class TestDelegationLifecycle:
    """TOOL_CALL_START opens an entry; TOOL_CALL_END closes it."""

    def test_open_and_close_delegation(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        # Open a delegation.
        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-1",
                "tool_call_name": "handoff",
                "metadata": {"sender": "orchestrator", "receiver": "worker-1"},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="evt-1",
        )

        entries = timeline.entries()
        assert len(entries) == 1
        assert entries[0].id == "tc-1"
        assert entries[0].kind == "delegation"
        assert entries[0].status == "open"
        assert entries[0].orchestration_type == "handoff"

        # Close it.
        timeline.handle_frame(
            AGUI_TOOL_CALL_END,
            {
                "tool_call_id": "tc-1",
                "tool_call_name": "handoff",
                "metadata": {},
                "timestamp": "2026-07-04T00:01:00Z",
            },
            event_id="evt-2",
        )

        entries = timeline.entries()
        assert len(entries) == 1
        assert entries[0].status == "completed"
        assert entries[0].ended_at == "2026-07-04T00:01:00Z"

    def test_tool_call_result_also_closes(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-2",
                "tool_call_name": "a2a_send",
                "metadata": {"sender": "s", "receiver": "r"},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="evt-1",
        )

        timeline.handle_frame(
            AGUI_TOOL_CALL_RESULT,
            {
                "tool_call_id": "tc-2",
                "result": "",
                "metadata": {},
                "timestamp": "2026-07-04T00:02:00Z",
            },
            event_id="evt-2",
        )

        entries = timeline.entries()
        assert entries[0].status == "completed"


class TestOrderingTiebreak:
    """entries() returns sorted by (started_at, id) for display."""

    def test_entries_sorted_by_started_at_then_id(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        # Insert in reverse order of timestamp.
        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-b",
                "tool_call_name": "handoff",
                "metadata": {},
                "timestamp": "2026-07-04T00:02:00Z",
            },
            event_id="evt-2",
        )
        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-a",
                "tool_call_name": "assign",
                "metadata": {},
                "timestamp": "2026-07-04T00:01:00Z",
            },
            event_id="evt-1",
        )

        entries = timeline.entries()
        assert entries[0].id == "tc-a"
        assert entries[1].id == "tc-b"

    def test_same_timestamp_tiebreaks_by_id(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-z",
                "tool_call_name": "handoff",
                "metadata": {},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="evt-1",
        )
        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-a",
                "tool_call_name": "assign",
                "metadata": {},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="evt-2",
        )

        entries = timeline.entries()
        assert entries[0].id == "tc-a"
        assert entries[1].id == "tc-z"


class TestUnknownCloserNoop:
    """Closing a tool_call_id that was never opened is a no-op."""

    def test_unknown_closer_no_error(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        # Close something that was never opened.
        timeline.handle_frame(
            AGUI_TOOL_CALL_END,
            {"tool_call_id": "unknown-id", "metadata": {}},
            event_id="evt-1",
        )

        assert timeline.entries() == []

    def test_result_for_unknown_no_error(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TOOL_CALL_RESULT,
            {"tool_call_id": "unknown-id", "result": "", "metadata": {}},
            event_id="evt-1",
        )

        assert timeline.entries() == []


class TestDuplicateStartNoop:
    """Duplicate TOOL_CALL_START with same tool_call_id is a no-op."""

    def test_duplicate_start_ignored(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        data = {
            "tool_call_id": "tc-1",
            "tool_call_name": "handoff",
            "metadata": {"sender": "s", "receiver": "r"},
            "timestamp": "2026-07-04T00:00:00Z",
        }

        timeline.handle_frame(AGUI_TOOL_CALL_START, data, event_id="evt-1")
        timeline.handle_frame(AGUI_TOOL_CALL_START, data, event_id="evt-2")

        entries = timeline.entries()
        assert len(entries) == 1


class TestFailureDisposition:
    """Failed metadata in closer marks entry as failed."""

    def test_failed_metadata_sets_failed_status(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-1",
                "tool_call_name": "handoff",
                "metadata": {},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="evt-1",
        )

        timeline.handle_frame(
            AGUI_TOOL_CALL_END,
            {
                "tool_call_id": "tc-1",
                "metadata": {"failed": True},
                "timestamp": "2026-07-04T00:05:00Z",
            },
            event_id="evt-2",
        )

        entries = timeline.entries()
        assert entries[0].status == "failed"

    def test_error_metadata_sets_failed_status(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-1",
                "tool_call_name": "assign",
                "metadata": {},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="evt-1",
        )

        timeline.handle_frame(
            AGUI_TOOL_CALL_END,
            {
                "tool_call_id": "tc-1",
                "metadata": {"error": True},
                "timestamp": "2026-07-04T00:10:00Z",
            },
            event_id="evt-2",
        )

        entries = timeline.entries()
        assert entries[0].status == "failed"


class TestCapEviction:
    """Retention cap evicts oldest entries first."""

    def test_cap_evicts_oldest(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter(), retention_cap=3)

        # Add 4 entries (cap is 3).
        for i in range(4):
            timeline.handle_frame(
                AGUI_TOOL_CALL_START,
                {
                    "tool_call_id": f"tc-{i}",
                    "tool_call_name": "handoff",
                    "metadata": {},
                    "timestamp": f"2026-07-04T00:0{i}:00Z",
                },
                event_id=f"evt-{i}",
            )

        entries = timeline.entries()
        assert len(entries) == 3
        # Oldest (tc-0) should have been evicted.
        ids = [e.id for e in entries]
        assert "tc-0" not in ids
        assert "tc-1" in ids
        assert "tc-2" in ids
        assert "tc-3" in ids

    def test_at_cap_boundary_no_eviction(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter(), retention_cap=3)

        # Add exactly 3 entries.
        for i in range(3):
            timeline.handle_frame(
                AGUI_TOOL_CALL_START,
                {
                    "tool_call_id": f"tc-{i}",
                    "tool_call_name": "handoff",
                    "metadata": {},
                    "timestamp": f"2026-07-04T00:0{i}:00Z",
                },
                event_id=f"evt-{i}",
            )

        entries = timeline.entries()
        assert len(entries) == 3


class TestDeltaNeverStored:
    """TEXT_MESSAGE_CONTENT never stores the delta text."""

    def test_message_entry_has_no_delta(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TEXT_MESSAGE_CONTENT,
            {
                "delta": "This is sensitive content that must not be stored",
                "metadata": {"sender": "orchestrator", "receiver": "user"},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="msg-1",
        )

        entries = timeline.entries()
        assert len(entries) == 1
        assert entries[0].kind == "message"
        assert entries[0].sender == "orchestrator"
        assert entries[0].receiver == "user"
        # The entry itself should not contain any delta/content field.
        entry_dict = entries[0].__dict__
        assert "delta" not in entry_dict or entry_dict.get("delta") is None

    def test_message_metadata_extracted(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TEXT_MESSAGE_CONTENT,
            {
                "delta": "",
                "sender": "agent-a",
                "receiver": "agent-b",
                "metadata": {"timestamp": "2026-07-04T01:00:00Z"},
            },
            event_id="msg-2",
        )

        entries = timeline.entries()
        assert entries[0].sender == "agent-a"
        assert entries[0].receiver == "agent-b"


class TestSeenSetDedup:
    """Seen_Set_Dedup skips id-bearing frames already processed."""

    def test_duplicate_event_id_skipped(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        data = {
            "tool_call_id": "tc-1",
            "tool_call_name": "handoff",
            "metadata": {},
            "timestamp": "2026-07-04T00:00:00Z",
        }

        timeline.handle_frame(AGUI_TOOL_CALL_START, data, event_id="evt-1")
        timeline.handle_frame(AGUI_TOOL_CALL_START, data, event_id="evt-1")

        entries = timeline.entries()
        assert len(entries) == 1

    def test_none_event_id_always_processed(self) -> None:
        timeline = MultiAgentSessionTimeline(_emitter())

        data = {
            "delta": "",
            "metadata": {"sender": "s", "receiver": "r"},
            "timestamp": "2026-07-04T00:00:00Z",
        }

        timeline.handle_frame(AGUI_TEXT_MESSAGE_CONTENT, data, event_id=None)
        timeline.handle_frame(AGUI_TEXT_MESSAGE_CONTENT, data, event_id=None)

        entries = timeline.entries()
        assert len(entries) == 2


class TestProjection:
    """projection() returns serializable dict of entries."""

    def test_projection_serializable(self) -> None:
        import json

        timeline = MultiAgentSessionTimeline(_emitter())

        timeline.handle_frame(
            AGUI_TOOL_CALL_START,
            {
                "tool_call_id": "tc-1",
                "tool_call_name": "handoff",
                "metadata": {"sender": "s", "receiver": "r"},
                "timestamp": "2026-07-04T00:00:00Z",
            },
            event_id="evt-1",
        )

        proj = timeline.projection()
        assert "entries" in proj
        # Must be JSON-serializable.
        serialized = json.dumps(proj)
        assert isinstance(json.loads(serialized), dict)
