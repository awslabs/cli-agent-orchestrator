"""SupervisorDashboardStream: fold-based L2 construct for fleet supervision.

Folds STATE_SNAPSHOT and STATE_DELTA frames to maintain a local copy of the
fleet topology (sessions + terminals), then derives supervisor-level views
(hierarchy, active counts, provider distribution, waiting terminals).

Design constraints:
- Pure fold: all state is derived from incoming frames; no I/O.
- STATE_DELTA before any snapshot is a no-op (never raises).
- Failed patch application drops the delta silently (never raises).
- Seen_Set_Dedup: id-bearing frames already processed are skipped;
  state frames (event_id=None) are always folded.
- Rollup counters track lifecycle events for supervisor observability.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.services.agui.base import (
    AguiConstruct,
    BoundedSeen,
    UiEmitter,
    apply_json_patch_strict,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_RUN_FINISHED,
    AGUI_RUN_STARTED,
    AGUI_STATE_DELTA,
    AGUI_STATE_SNAPSHOT,
    AGUI_STEP_FINISHED,
    AGUI_STEP_STARTED,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TOOL_CALL_START,
)

# Frame types that update rollup counters.
_ROLLUP_TYPES = frozenset(
    {
        AGUI_STEP_STARTED,
        AGUI_STEP_FINISHED,
        AGUI_RUN_STARTED,
        AGUI_RUN_FINISHED,
        AGUI_TOOL_CALL_START,
        AGUI_TOOL_CALL_END,
        AGUI_TOOL_CALL_RESULT,
    }
)


class SupervisorDashboardStream(AguiConstruct):
    """Fold-based supervisor view over the AG-UI fleet state stream.

    Consumes STATE_SNAPSHOT/STATE_DELTA frames to maintain a local copy of the
    fleet topology and derives hierarchy/supervisor_snapshot views on demand.

    Usage::

        dashboard = SupervisorDashboardStream(emitter)
        for agui_type, data, event_id in frames:
            dashboard.handle_frame(agui_type, data, event_id)
        view = dashboard.supervisor_snapshot()
    """

    def __init__(self, emitter: UiEmitter) -> None:
        super().__init__(emitter)
        # Fleet state derived from STATE_SNAPSHOT/STATE_DELTA frames.
        self._fleet: Optional[Dict[str, Any]] = None
        # Bounded seen set for deduplication of id-bearing frames.
        self._seen = BoundedSeen()
        # Rollup counters for lifecycle events.
        self._rollup: Dict[str, int] = {
            AGUI_STEP_STARTED: 0,
            AGUI_STEP_FINISHED: 0,
            AGUI_RUN_STARTED: 0,
            AGUI_RUN_FINISHED: 0,
            AGUI_TOOL_CALL_START: 0,
            AGUI_TOOL_CALL_END: 0,
            AGUI_TOOL_CALL_RESULT: 0,
        }
        # Track the most recent activity.
        self._last_activity: Optional[Dict[str, Any]] = None

    def handle_frame(
        self, agui_type: str, data: Dict[str, Any], event_id: Optional[str] = None
    ) -> None:
        """Process one AG-UI frame.

        STATE_SNAPSHOT: deep-copy replace self._fleet.
        STATE_DELTA: apply patch via apply_json_patch_strict (drop on failure).
        Id-bearing lifecycle frames: update rollup counters.
        Frames with event_id already seen: skip (dedup).
        State frames (event_id=None): always folded.
        """
        # Seen_Set_Dedup: skip id-bearing frames already processed.
        if event_id is not None:
            if event_id in self._seen:
                return
            self._seen.add(event_id)

        # Track last activity for all processed frames.
        if event_id is not None:
            self._last_activity = {
                "timestamp": data.get("timestamp"),
                "event_id": event_id,
            }

        if agui_type == AGUI_STATE_SNAPSHOT:
            self._fleet = copy.deepcopy(data.get("snapshot", data))
            return

        if agui_type == AGUI_STATE_DELTA:
            if self._fleet is None:
                # Delta before snapshot is a no-op.
                return
            delta = data.get("delta", [])
            result = apply_json_patch_strict(self._fleet, delta)
            if result is not None:
                self._fleet = result
            # Failed patch: drop silently.
            return

        # Rollup counters for lifecycle events.
        if agui_type in _ROLLUP_TYPES:
            self._rollup[agui_type] = self._rollup.get(agui_type, 0) + 1

    def hierarchy(self) -> Dict[str, Dict[str, Any]]:
        """Derive session hierarchy from fleet state.

        Returns a dict mapping session_name -> {status, terminal_ids, terminal_count}.
        Groups terminals by their session_name field.
        """
        if self._fleet is None:
            return {}

        sessions: List[Dict[str, Any]] = self._fleet.get("sessions", [])
        terminals: List[Dict[str, Any]] = self._fleet.get("terminals", [])

        result: Dict[str, Dict[str, Any]] = {}

        # Initialize from sessions.
        for session in sessions:
            name = session.get("name", session.get("id", ""))
            result[name] = {
                "status": session.get("status", "unknown"),
                "terminal_ids": [],
                "terminal_count": 0,
            }

        # Group terminals by session_name.
        for terminal in terminals:
            session_name = terminal.get("session_name", "")
            if session_name not in result:
                # Terminal references a session not in the sessions list;
                # create an entry with unknown status.
                result[session_name] = {
                    "status": "unknown",
                    "terminal_ids": [],
                    "terminal_count": 0,
                }
            result[session_name]["terminal_ids"].append(terminal.get("id", ""))
            result[session_name]["terminal_count"] += 1

        return result

    def supervisor_snapshot(self) -> Dict[str, Any]:
        """Return a supervisor-level summary of the fleet.

        Returns:
            {
                "active_sessions": int - sessions not in terminated state,
                "counts": dict - from fleet counts or zeros,
                "by_provider": dict - terminal count per provider,
                "waiting_terminals": list - terminal ids with status=="waiting_user_answer",
                "last_activity": dict or None - {timestamp, event_id} of most recent frame,
            }
        """
        if self._fleet is None:
            return {
                "active_sessions": 0,
                "counts": {"sessions": 0, "terminals": 0},
                "by_provider": {},
                "waiting_terminals": [],
                "last_activity": self._last_activity,
            }

        sessions: List[Dict[str, Any]] = self._fleet.get("sessions", [])
        terminals: List[Dict[str, Any]] = self._fleet.get("terminals", [])
        counts = self._fleet.get(
            "counts",
            {
                "sessions": len(sessions),
                "terminals": len(terminals),
            },
        )

        # Active sessions: not terminated.
        active_sessions = sum(
            1 for s in sessions if s.get("status") not in ("terminated", "closed")
        )

        # Provider distribution.
        by_provider: Dict[str, int] = {}
        for terminal in terminals:
            provider = terminal.get("provider", "unknown")
            by_provider[provider] = by_provider.get(provider, 0) + 1

        # Waiting terminals.
        waiting_terminals = [
            t.get("id", "") for t in terminals if t.get("status") == "waiting_user_answer"
        ]

        return {
            "active_sessions": active_sessions,
            "counts": counts,
            "by_provider": by_provider,
            "waiting_terminals": waiting_terminals,
            "last_activity": self._last_activity,
        }

    def projection(self) -> Dict[str, Any]:
        """Return the supervisor_snapshot dict as the projection."""
        return self.supervisor_snapshot()
