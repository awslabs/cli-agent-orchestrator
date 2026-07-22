"""TOOL_CALL lifecycle tracker for the AG-UI stream.

Correlates TOOL_CALL_START frames with their corresponding completion records
and synthesizes TOOL_CALL_END (and optionally TOOL_CALL_RESULT) closer frames.

Design constraints:
- Deterministic: same input records always produce same output frames (replay-safe).
- Bounded: capped open-call map with oldest-first eviction (no unbounded growth).
- No orphan closers: a completion record that does not match any open call
  produces no synthesized closer frame.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STEP_FINISHED,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TOOL_CALL_START,
)

# Type alias for an AG-UI frame: (agui_type, data_dict)
Frame = Tuple[str, Dict[str, Any]]

# Default cap on open entries to prevent unbounded growth.
DEFAULT_MAX_OPEN = 256


class ToolCallLifecycleTracker:
    """Tracks open TOOL_CALL_START frames and synthesizes closers.

    Usage::

        tracker = ToolCallLifecycleTracker()
        for record in records:
            mapped_frame = to_agui_event(record)
            frames = tracker.feed(record, mapped_frame)
            for frame in frames:
                yield frame

    The tracker maintains an ordered mapping of ``receiver_terminal_id -> open_call_info``
    for every TOOL_CALL_START frame seen. When a completion record arrives whose
    ``terminal_id`` matches an open receiver, the tracker synthesizes a TOOL_CALL_END
    frame. For ``a2a_delegation`` opens, a TOOL_CALL_RESULT is also synthesized.

    The ``close_all()`` method can be called at session end to synthesize closers
    for all remaining open calls.
    """

    def __init__(self, max_open: int = DEFAULT_MAX_OPEN) -> None:
        self._max_open = max_open
        # OrderedDict preserves insertion order for oldest-first eviction.
        # Key: receiver terminal_id -> value: dict with tool_call_id, tool_call_name, metadata
        self._open: OrderedDict[str, Dict[str, Any]] = OrderedDict()

    @property
    def open_count(self) -> int:
        """Number of currently tracked open tool calls."""
        return len(self._open)

    def feed(self, record: Dict[str, Any], mapped_frame: Frame) -> List[Frame]:
        """Process one record and its already-mapped AG-UI frame.

        Returns a list of frames to emit: the original mapped_frame first, then
        any synthesized closer frames.

        Args:
            record: The original CAO event record (with kind, terminal_id, detail, etc.)
            mapped_frame: The (agui_type, data) pair from ``to_agui_event(record)``

        Returns:
            List of (agui_type, data) frames to emit in order.
        """
        agui_type, data = mapped_frame
        frames: List[Frame] = [mapped_frame]

        # Track new TOOL_CALL_START opens.
        if agui_type == AGUI_TOOL_CALL_START:
            superseded = self._track_open(record, data)
            # Superseded closers come before the new open frame.
            return superseded + frames

        # Check for completion that closes an open tool call.
        if agui_type == AGUI_STEP_FINISHED:
            terminal_id = record.get("terminal_id")
            closers = self._try_close(terminal_id, record)
            frames.extend(closers)

        return frames

    def close_all(self) -> List[Frame]:
        """Synthesize TOOL_CALL_END for all remaining open calls (session end).

        Returns frames in insertion order (oldest first) for determinism.
        """
        frames: List[Frame] = []
        # Iterate over a copy of keys since we modify the dict.
        for receiver_id in list(self._open.keys()):
            closers = self._synthesize_close(receiver_id, timestamp=None, closed_by="session_end")
            frames.extend(closers)
        return frames

    def _track_open(self, record: Dict[str, Any], data: Dict[str, Any]) -> List[Frame]:
        """Register a new open tool call, with bounded eviction.

        If the receiver already has an open call, that call is closed first
        (synthesized TOOL_CALL_END with disposition "superseded") before
        registering the new one.

        Returns any synthesized closer frames for a superseded open call.
        """
        detail: Dict[str, Any] = record.get("detail") or {}
        receiver = detail.get("receiver")
        if not receiver:
            # Cannot track without a receiver terminal_id.
            return []

        superseded_frames: List[Frame] = []

        # If there is already an open call for this receiver, close it first.
        if receiver in self._open:
            superseded_frames = self._synthesize_close(
                receiver,
                timestamp=record.get("timestamp"),
                closed_by="superseded",
                disposition="superseded",
            )

        # Evict oldest entries if at capacity.
        while len(self._open) >= self._max_open:
            self._open.popitem(last=False)

        self._open[receiver] = {
            "tool_call_id": data.get("tool_call_id") or record.get("id"),
            "tool_call_name": data.get("tool_call_name"),
            "kind": record.get("kind"),
            "metadata": data.get("metadata") or {},
        }

        return superseded_frames

    def _try_close(self, terminal_id: Optional[str], record: Dict[str, Any]) -> List[Frame]:
        """Attempt to close an open tool call matching the terminal_id."""
        if terminal_id is None or terminal_id not in self._open:
            # No orphan closers: if we don't know about this terminal, emit nothing.
            return []
        return self._synthesize_close(terminal_id, timestamp=record.get("timestamp"))

    def _synthesize_close(
        self,
        receiver_id: str,
        timestamp: Optional[str] = None,
        closed_by: str = "completion",
        disposition: Optional[str] = None,
    ) -> List[Frame]:
        """Synthesize TOOL_CALL_END (and optionally TOOL_CALL_RESULT) for a receiver.

        Args:
            receiver_id: The terminal_id of the receiver to close.
            timestamp: Optional timestamp for the synthesized frames.
            closed_by: Why the call was closed — ``"completion"`` (receiver
                finished its turn), ``"session_end"`` (close_all at teardown),
                or ``"superseded"`` (a new open replaced it). Recorded on the
                closer metadata so clients can distinguish a real completion
                from a lifecycle-forced close (R6.3).
            disposition: Optional legacy disposition string (kept for
                back-compat; set alongside ``closed_by`` for superseded closes).
        """
        open_info = self._open.pop(receiver_id, None)
        if open_info is None:  # pragma: no cover - callers guard membership first
            return []

        tool_call_id = open_info["tool_call_id"]
        metadata = dict(open_info["metadata"])
        metadata["closed_by"] = closed_by
        if disposition:
            metadata["disposition"] = disposition
        frames: List[Frame] = []

        # For a2a_delegation opens, also synthesize TOOL_CALL_RESULT.
        if open_info["kind"] == "a2a_delegation":
            frames.append(
                (
                    AGUI_TOOL_CALL_RESULT,
                    {
                        "tool_call_id": tool_call_id,
                        "result": "",  # Metadata-only: no body content.
                        "metadata": metadata,
                        "timestamp": timestamp,
                    },
                )
            )

        # Always synthesize TOOL_CALL_END.
        frames.append(
            (
                AGUI_TOOL_CALL_END,
                {
                    "tool_call_id": tool_call_id,
                    "tool_call_name": open_info["tool_call_name"],
                    "metadata": metadata,
                    "timestamp": timestamp,
                },
            )
        )

        return frames
