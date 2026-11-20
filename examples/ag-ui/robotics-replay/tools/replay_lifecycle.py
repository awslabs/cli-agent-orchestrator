"""Lifecycle completeness for a recorded AG-UI stream. Standard library only.

The recorder's stream listener appends frames on a daemon thread, so when the
scenario's last step returns, the records that close its tool calls and subagent
runs may still be in flight. ``wait_until_closed`` polls, within a bound, until
every lifecycle the recording opened is closed. ``unclosed`` is the check the
recorder runs on the frames it is about to write, so a trace with a lifecycle
left open is refused instead of written as a successful recording.

Kept apart from ``record_robotics_trace.py`` (which imports mujoco) so the
example's test can import it.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Iterable, List

# Opening type -> (closing type, id field).
PAIRS = {
    "TOOL_CALL_START": ("TOOL_CALL_END", "toolCallId"),
    "SUBAGENT_STARTED": ("SUBAGENT_FINISHED", "subagentRunId"),
}


def unclosed(frames: Iterable[Dict[str, Any]], kinds: Iterable[str] = tuple(PAIRS)) -> List[str]:
    """One message per lifecycle opened in ``frames`` and not closed, in open order."""
    wanted = {k: PAIRS[k] for k in kinds}
    closing = {end: (start, key) for start, (end, key) in wanted.items()}
    open_ids: Dict[tuple, str] = {}
    for f in frames:
        t = f.get("type")
        if t in wanted:
            key = wanted[t][1]
            open_ids[(t, f.get(key))] = f"{t} {f.get(key)} has no {wanted[t][0]}"
        elif t in closing:
            start, key = closing[t]
            open_ids.pop((start, f.get(key)), None)
    return list(open_ids.values())


def wait_until_closed(
    get_frames: Callable[[], List[Dict[str, Any]]],
    bound: float,
    *,
    what: str,
    kinds: Iterable[str] = tuple(PAIRS),
    poll: float = 0.05,
) -> None:
    """Return once ``unclosed(get_frames(), kinds)`` is empty; raise TimeoutError
    naming what is still open if that takes longer than ``bound`` seconds."""
    kinds = tuple(kinds)
    deadline = time.monotonic() + bound
    while True:
        left = unclosed(get_frames(), kinds)
        if not left:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{what} not received within {bound:.0f}s: " + "; ".join(left))
        time.sleep(poll)
