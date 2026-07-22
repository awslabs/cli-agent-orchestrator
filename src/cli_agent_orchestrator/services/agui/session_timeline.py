"""MultiAgentSessionTimeline: fold-based L2 construct for delegation/message tracking.

Tracks TOOL_CALL_START (open delegation entries), TOOL_CALL_END/TOOL_CALL_RESULT
(close matching entries), and TEXT_MESSAGE_CONTENT (message entries). Never stores
delta text content -- only metadata about the message event.

Design constraints:
- Frozen TimelineEntry dataclass for immutable entries.
- Seen_Set_Dedup: id-bearing frames already processed are skipped.
- Retention cap (default 1000, constructor-configurable): evicts oldest first.
- entries() returns list sorted by (started_at, id) for display tiebreak.
- Duplicate TOOL_CALL_START (same tool_call_id): no-op.
- Unknown closer (TOOL_CALL_END/RESULT for unknown id): no-op.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.services.agui.base import AguiConstruct, BoundedSeen, UiEmitter
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_TEXT_MESSAGE_CONTENT,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TOOL_CALL_START,
)

# Default retention cap.
DEFAULT_RETENTION_CAP = 1000


@dataclass(frozen=True)
class TimelineEntry:
    """A single timeline entry (delegation or message)."""

    id: str
    kind: str  # "delegation" or "message"
    orchestration_type: Optional[str] = None
    sender: Optional[str] = None
    receiver: Optional[str] = None
    tool_call_name: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    status: str = "open"  # "open", "completed", "failed"


class MultiAgentSessionTimeline(AguiConstruct):
    """Fold-based timeline of multi-agent delegations and messages.

    Consumes TOOL_CALL_START, TOOL_CALL_END/RESULT, and TEXT_MESSAGE_CONTENT
    frames to build an ordered timeline of inter-agent interactions.

    Usage::

        timeline = MultiAgentSessionTimeline(emitter)
        for agui_type, data, event_id in frames:
            timeline.handle_frame(agui_type, data, event_id)
        entries = timeline.entries()
    """

    def __init__(self, emitter: UiEmitter, retention_cap: int = DEFAULT_RETENTION_CAP) -> None:
        super().__init__(emitter)
        self._retention_cap = retention_cap
        # Ordered list of entries (arrival order).
        self._entries: List[TimelineEntry] = []
        # Index of open delegations by tool_call_id for fast lookup.
        self._open_by_id: Dict[str, int] = {}
        # Bounded seen set for deduplication of id-bearing frames.
        self._seen = BoundedSeen()
        # Bounded set of tool_call_ids we have opened (to detect duplicate starts).
        self._started_ids = BoundedSeen()

    def handle_frame(
        self, agui_type: str, data: Dict[str, Any], event_id: Optional[str] = None
    ) -> None:
        """Process one AG-UI frame.

        TOOL_CALL_START: open a new delegation entry.
        TOOL_CALL_END/RESULT: close matching open entry.
        TEXT_MESSAGE_CONTENT: append a message entry (never stores delta).
        """
        # Seen_Set_Dedup: skip id-bearing frames already processed.
        if event_id is not None:
            if event_id in self._seen:
                return
            self._seen.add(event_id)

        if agui_type == AGUI_TOOL_CALL_START:
            self._handle_start(data)
        elif agui_type in (AGUI_TOOL_CALL_END, AGUI_TOOL_CALL_RESULT):
            self._handle_close(data, agui_type)
        elif agui_type == AGUI_TEXT_MESSAGE_CONTENT:
            self._handle_message(data, event_id)

    def _handle_start(self, data: Dict[str, Any]) -> None:
        """Open a new delegation entry keyed by tool_call_id."""
        tool_call_id = data.get("tool_call_id", "")
        if not tool_call_id:
            return

        # Duplicate start: no-op.
        if tool_call_id in self._started_ids:
            return
        self._started_ids.add(tool_call_id)

        metadata = data.get("metadata", {})
        entry = TimelineEntry(
            id=tool_call_id,
            kind="delegation",
            orchestration_type=data.get("tool_call_name"),
            sender=metadata.get("sender"),
            receiver=metadata.get("receiver"),
            tool_call_name=data.get("tool_call_name"),
            started_at=data.get("timestamp") or metadata.get("timestamp"),
            status="open",
        )

        self._entries.append(entry)
        self._open_by_id[tool_call_id] = len(self._entries) - 1
        self._enforce_cap()

    def _handle_close(self, data: Dict[str, Any], agui_type: str) -> None:
        """Close a matching open delegation entry."""
        tool_call_id = data.get("tool_call_id", "")
        if not tool_call_id:
            return

        # Unknown closer: no-op.
        if tool_call_id not in self._open_by_id:
            return

        idx = self._open_by_id.pop(tool_call_id)
        if idx >= len(self._entries):  # pragma: no cover - idx from _open_by_id is always valid
            return

        old_entry = self._entries[idx]
        # Determine new status.
        metadata = data.get("metadata", {})
        failed = metadata.get("failed", False) or metadata.get("error", False)
        new_status = "failed" if failed else "completed"

        # Replace frozen entry with updated version.
        self._entries[idx] = TimelineEntry(
            id=old_entry.id,
            kind=old_entry.kind,
            orchestration_type=old_entry.orchestration_type,
            sender=old_entry.sender,
            receiver=old_entry.receiver,
            tool_call_name=old_entry.tool_call_name,
            started_at=old_entry.started_at,
            ended_at=data.get("timestamp") or metadata.get("timestamp"),
            status=new_status,
        )

    def _handle_message(self, data: Dict[str, Any], event_id: Optional[str]) -> None:
        """Append a message entry. Never stores delta content."""
        metadata = data.get("metadata", {})
        entry_id = event_id or data.get("message_id", f"msg-{len(self._entries)}")

        entry = TimelineEntry(
            id=entry_id,
            kind="message",
            orchestration_type=None,
            sender=metadata.get("sender") or data.get("sender"),
            receiver=metadata.get("receiver") or data.get("receiver"),
            tool_call_name=None,
            started_at=data.get("timestamp") or metadata.get("timestamp"),
            ended_at=None,
            status="completed",
        )

        self._entries.append(entry)
        self._enforce_cap()

    def _enforce_cap(self) -> None:
        """Evict oldest entries to stay within retention cap."""
        while len(self._entries) > self._retention_cap:
            evicted = self._entries.pop(0)
            # Clean up index references.
            if evicted.id in self._open_by_id:
                del self._open_by_id[evicted.id]
            # Rebuild index since indices shifted.
            self._rebuild_index()

    def _rebuild_index(self) -> None:
        """Rebuild open_by_id index after eviction."""
        self._open_by_id = {}
        for i, entry in enumerate(self._entries):
            if entry.kind == "delegation" and entry.status == "open":
                self._open_by_id[entry.id] = i

    def entries(self) -> List[TimelineEntry]:
        """Return list of entries sorted by (started_at, id) for display.

        Entries with None started_at sort before entries with timestamps.
        """
        return sorted(
            self._entries,
            key=lambda e: (e.started_at or "", e.id),
        )

    def projection(self) -> Dict[str, Any]:
        """Return serializable list of timeline entries."""
        return {"entries": [asdict(entry) for entry in self.entries()]}
