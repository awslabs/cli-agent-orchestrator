"""Client-side SSE frame parsing for the workflow live-event follower (issue #505, U10).

CONSUMER-ONLY. This module parses #504's FINAL events-follow SSE frame contract
(``GET /workflows/runs/{run_id}/events`` with ``Accept: text/event-stream``) into
typed :class:`SseFrame` values. It is a pure line parser: it imports only stdlib,
takes the already-decoded line iterator handed in by the caller, and reaches NO
engine / journal / event DAL (FR-7.4 ownership split — #505 builds only the
follower, never #504's route or read DAL).

LOAD-BEARING INVARIANT (GD-1, render-not-infer): a gap is a SERVER-DECLARED
``event: gap`` frame. This module NEVER computes a gap from ``seq`` arithmetic —
there is deliberately no ``seq != last_seq + 1`` synthesis anywhere here or in the
followers that consume it. An independent client inference could disagree with the
server about whether an event was lost, so the server's declaration is the sole
authority; the follower renders exactly what the ``event: gap`` frame carries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, Optional

# The terminal projection states a frame's ``state`` field may carry. The follower
# closes on EITHER — a terminal ``event:`` line OR a terminal ``state`` in the
# frame data — so a swallowed terminal EVENT can never hang the follow. The
# ``event:``-side membership test is ``_EVENT_TO_STATE`` below, which is the single
# authority for it: a sibling ``TERMINAL_EVENT_TYPES`` frozenset was exported here
# with no referent anywhere in src/ or test/ (PR #525 review) and is removed, since
# two overlapping definitions of "which events are terminal" can silently disagree.
TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})

# The declared-gap frame's ``event:`` value. A gap frame carries no ``id:`` line
# (per the contract), so it never advances the reconnect cursor.
GAP_EVENT_TYPE = "gap"

# Maps a terminal run-level event type to the run state it settles.
_EVENT_TO_STATE = {
    "run.completed": "completed",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}


@dataclass
class SseFrame:
    """One dispatched SSE frame: its ``event:`` name, parsed ``data:`` object, ``id:``.

    ``data`` is always a dict (a non-object JSON payload is wrapped under
    ``_raw``). ``id`` is the raw ``id:`` string (the per-run ``seq`` for a normal
    frame) or ``None`` (a gap frame carries no ``id:``).
    """

    event: Optional[str]
    data: Dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None

    @property
    def is_gap(self) -> bool:
        """True iff this is a server-DECLARED gap frame (``event: gap``)."""
        return self.event == GAP_EVENT_TYPE

    @property
    def terminal_state(self) -> Optional[str]:
        """The terminal RUN state this frame settles, or ``None`` if non-terminal.

        Run-level only — a terminal ``run.*`` event is authoritative. A
        ``step.completed`` frame carries ``state: "completed"`` (the STEP's state,
        not the run's); treating that as terminal would close the follow one step
        early, so a state-only match is honored ONLY on a run-projection frame
        (``step_id`` null/absent). This is the F-1 terminal guard's frame side: a
        swallowed terminal event is still caught by the follower's status-check
        path, never by mis-reading a step state as the run's. A gap frame is never
        terminal.
        """
        if self.is_gap:
            return None
        # A run-level terminal event is authoritative regardless of ``state``.
        if self.event in _EVENT_TO_STATE:
            return _EVENT_TO_STATE[self.event]
        # Otherwise only a run-projection frame (no step_id) whose state is a
        # terminal run state settles the run.
        if self.data.get("step_id") is None:
            state = self.data.get("state")
            if isinstance(state, str) and state in TERMINAL_RUN_STATES:
                return state
        return None

    @property
    def is_terminal(self) -> bool:
        """True iff this frame ends the follow (terminal event OR terminal state)."""
        return self.terminal_state is not None

    def seq(self) -> Optional[int]:
        """The frame's per-run ``seq`` (from the ``id:`` line), or ``None``.

        The reconnect cursor. Advanced only on normal frames (a gap frame has no
        ``id:``), so resume via ``?after_seq=<seq>`` is exact and dedupe-free.
        """
        if self.id is None:
            return None
        try:
            return int(self.id)
        except (TypeError, ValueError):
            return None


def _build_frame(
    event: Optional[str], data_buf: Optional[str], frame_id: Optional[str]
) -> Optional[SseFrame]:
    """Assemble one accumulated SSE event into a frame, or ``None`` to skip it.

    A frame with a ``data:`` payload that is not parseable JSON is skipped
    (never crashes the follow), matching the AG-UI reader's leniency. An empty
    accumulator (only blank lines / comments seen) yields ``None``.
    """
    if event is None and data_buf is None and frame_id is None:
        return None
    parsed: Dict[str, Any] = {}
    if data_buf is not None:
        try:
            obj = json.loads(data_buf)
        except (json.JSONDecodeError, ValueError):
            return None
        parsed = obj if isinstance(obj, dict) else {"_raw": obj}
    return SseFrame(event=event, data=parsed, id=frame_id)


def parse_sse_frames(lines: Iterable[Optional[str]]) -> Iterator[SseFrame]:
    """Parse decoded SSE lines into :class:`SseFrame` values (blank line = dispatch).

    ``lines`` is the iterator returned by
    ``requests.Response.iter_lines(decode_unicode=True)`` (trailing newline
    already stripped). Comment/heartbeat lines (``:``-prefixed) are ignored;
    ``id:`` / ``event:`` / ``data:`` accumulate until a blank line dispatches the
    frame. A ``data:`` field spanning multiple lines is concatenated with ``\\n``
    per the SSE spec. Any transport error raised by the underlying iterator
    propagates to the caller (the follower catches it to reconnect).
    """
    event: Optional[str] = None
    data_buf: Optional[str] = None
    frame_id: Optional[str] = None

    for raw in lines:
        if raw is None:
            continue
        line = raw

        if line == "":
            frame = _build_frame(event, data_buf, frame_id)
            if frame is not None:
                yield frame
            event = data_buf = frame_id = None
            continue

        # SSE comment / heartbeat line.
        if line.startswith(":"):
            continue

        colon = line.find(":")
        if colon < 0:
            # Malformed field line (no colon) — skip.
            continue

        field_name = line[:colon]
        value = line[colon + 1 :]
        if value.startswith(" "):
            value = value[1:]

        if field_name == "event":
            event = value
        elif field_name == "data":
            data_buf = value if data_buf is None else f"{data_buf}\n{value}"
        elif field_name == "id":
            frame_id = value
        # Other fields (retry, …) are ignored.

    # A stream that ends without a trailing blank line still dispatches the last
    # accumulated frame.
    frame = _build_frame(event, data_buf, frame_id)
    if frame is not None:
        yield frame


__all__ = [
    "GAP_EVENT_TYPE",
    "SseFrame",
    "TERMINAL_RUN_STATES",
    "parse_sse_frames",
]
