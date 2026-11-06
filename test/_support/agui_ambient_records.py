"""Deterministic CAO event records for the ambient AG-UI stream tests.

One record per primitive (and the generative-UI and legacy-envelope shapes), in
the bus/EventLog record format ``agui_stream.to_agui_event`` consumes. Shared by
the internal-frame golden, the legacy-framing byte-identity golden and the
conformance tests, so all three exercise the same inputs.
"""

from __future__ import annotations

from typing import Any, Dict, List

SESSION = "cao-fleet-demo"


def _rec(rid: str, kind: str, ts: str, terminal_id: Any, detail: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rid,
        "kind": kind,
        "terminal_id": terminal_id,
        "session_name": SESSION,
        "timestamp": ts,
        "detail": detail,
    }


SESSION_LAUNCH = _rec(
    "evt-01",
    "launch",
    "2026-11-04T10:00:00.000000+00:00",
    None,
    {"event_type": "post_create_session"},
)
TERMINAL_A_LAUNCH = _rec(
    "evt-02",
    "launch",
    "2026-11-04T10:00:01.000000+00:00",
    "aaaa1111",
    {"event_type": "post_create_terminal", "agent_name": "supervisor", "provider": "kiro_cli"},
)
TERMINAL_B_LAUNCH = _rec(
    "evt-03",
    "launch",
    "2026-11-04T10:00:02.000000+00:00",
    "bbbb2222",
    {"event_type": "post_create_terminal", "agent_name": "developer", "provider": "kiro_cli"},
)
HANDOFF = _rec(
    "evt-04",
    "handoff",
    "2026-11-04T10:00:03.000000+00:00",
    "aaaa1111",
    {"sender": "aaaa1111", "receiver": "bbbb2222", "orchestration_type": "handoff"},
)
SEND_MESSAGE = _rec(
    "evt-05",
    "handoff",
    "2026-11-04T10:00:04.000000+00:00",
    "aaaa1111",
    {"sender": "aaaa1111", "receiver": "bbbb2222", "orchestration_type": "send_message"},
)
A2A = _rec(
    "evt-06",
    "a2a_delegation",
    "2026-11-04T10:00:05.000000+00:00",
    "aaaa1111",
    {"sender": "aaaa1111", "receiver": "bbbb2222", "orchestration_type": "a2a"},
)
FILE_MOD = _rec(
    "evt-07",
    "file_mod",
    "2026-11-04T10:00:06.000000+00:00",
    "bbbb2222",
    {"event_type": "file_modified", "path": "src/app.py"},
)
GENERATIVE_UI = _rec(
    "evt-08",
    "other",
    "2026-11-04T10:00:07.000000+00:00",
    "bbbb2222",
    {
        "event_type": "agent_ui",
        "ui": {"component": "metric", "props": {"label": "tests", "value": 3}},
    },
)
OFF_LIST_UI = _rec(
    "evt-09",
    "other",
    "2026-11-04T10:00:08.000000+00:00",
    "bbbb2222",
    {"event_type": "agent_ui", "ui": {"component": "html_blob", "props": {"html": "<b>x</b>"}}},
)
TERMINAL_ERROR = _rec(
    "evt-10",
    "error",
    "2026-11-04T10:00:09.000000+00:00",
    "bbbb2222",
    {"event_type": "provider_error", "message": "redacted"},
)
OTHER = _rec(
    "evt-11", "other", "2026-11-04T10:00:10.000000+00:00", None, {"event_type": "flow_tick"}
)
TERMINAL_B_KILL = _rec(
    "evt-12",
    "completion",
    "2026-11-04T10:00:11.000000+00:00",
    "bbbb2222",
    {"event_type": "post_kill_terminal", "agent_name": "developer"},
)
SESSION_KILL = _rec(
    "evt-13",
    "completion",
    "2026-11-04T10:00:12.000000+00:00",
    None,
    {"event_type": "post_kill_session"},
)
LEGACY_ENVELOPE = {
    "type": "terminal.created",
    "payload": {"terminal_id": "cccc3333", "agent_name": "reviewer", "provider": "kiro_cli"},
    "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
}

PRIMITIVE_RECORDS: List[Dict[str, Any]] = [
    SESSION_LAUNCH,
    TERMINAL_A_LAUNCH,
    TERMINAL_B_LAUNCH,
    HANDOFF,
    SEND_MESSAGE,
    A2A,
    FILE_MOD,
    GENERATIVE_UI,
    OFF_LIST_UI,
    TERMINAL_ERROR,
    OTHER,
    TERMINAL_B_KILL,
    SESSION_KILL,
]

# Tracker feed sequence: open a handoff and an a2a call on the receiver, then the
# receiver's kill closes them (a2a synthesizes TOOL_CALL_RESULT + TOOL_CALL_END).
TRACKER_SEQUENCE: List[Dict[str, Any]] = [
    TERMINAL_A_LAUNCH,
    TERMINAL_B_LAUNCH,
    HANDOFF,
    A2A,
    TERMINAL_B_KILL,
]
