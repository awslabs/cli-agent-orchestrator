#!/usr/bin/env bash
#
# SupervisorDashboardStream example.
#
# Demonstrates the fold-based supervisor dashboard construct by feeding
# synthetic AG-UI frames directly (no live server required). This dogfoods
# the library's own classes: RecordingUiEmitter + SupervisorDashboardStream.
#
# Usage:
#   ./examples/ag-ui/ag-ui-supervisor-dashboard/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

cleanup() {
    local code=$?
    exit "${code}"
}
trap cleanup EXIT INT TERM

echo "[supervisor-dashboard] Running SupervisorDashboardStream example..." >&2

uv run python3 - <<'PYTHON'
"""Demonstrate SupervisorDashboardStream fold composition."""
import json
import sys

from cli_agent_orchestrator.services.agui import (
    RecordingUiEmitter,
    SupervisorDashboardStream,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STATE_SNAPSHOT,
    AGUI_STATE_DELTA,
    AGUI_STEP_STARTED,
    AGUI_STEP_FINISHED,
    AGUI_TOOL_CALL_START,
)

emitter = RecordingUiEmitter()
dashboard = SupervisorDashboardStream(emitter)

# 1. Feed a STATE_SNAPSHOT with two sessions and three terminals.
snapshot = {
    "sessions": [
        {"name": "session-alpha", "status": "active", "id": "s1"},
        {"name": "session-beta", "status": "active", "id": "s2"},
    ],
    "terminals": [
        {"id": "t1", "session_name": "session-alpha", "provider": "claude_code", "status": "working"},
        {"id": "t2", "session_name": "session-alpha", "provider": "kiro_cli", "status": "waiting_user_answer"},
        {"id": "t3", "session_name": "session-beta", "provider": "codex", "status": "working"},
    ],
    "counts": {"sessions": 2, "terminals": 3},
}
dashboard.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}, event_id=None)
print("[1] After STATE_SNAPSHOT:")
print(f"    hierarchy = {json.dumps(dashboard.hierarchy(), indent=2)}")
print(f"    snapshot  = {json.dumps(dashboard.supervisor_snapshot(), indent=2)}")

# 2. Feed lifecycle frames (rollup counters).
dashboard.handle_frame(AGUI_STEP_STARTED, {"terminal_id": "t1"}, event_id="ev-1")
dashboard.handle_frame(AGUI_STEP_STARTED, {"terminal_id": "t3"}, event_id="ev-2")
dashboard.handle_frame(AGUI_TOOL_CALL_START, {"tool_call_id": "tc-1"}, event_id="ev-3")
dashboard.handle_frame(AGUI_STEP_FINISHED, {"terminal_id": "t1"}, event_id="ev-4")
print("\n[2] After lifecycle frames (rollup):")
snap = dashboard.supervisor_snapshot()
print(f"    last_activity = {snap['last_activity']}")

# 3. Feed a STATE_DELTA: terminate session-beta.
delta_ops = [
    {"op": "replace", "path": "/sessions/1/status", "value": "terminated"},
]
dashboard.handle_frame(AGUI_STATE_DELTA, {"delta": delta_ops}, event_id=None)
print("\n[3] After STATE_DELTA (session-beta terminated):")
snap = dashboard.supervisor_snapshot()
print(f"    active_sessions = {snap['active_sessions']}")
print(f"    by_provider     = {snap['by_provider']}")
print(f"    waiting         = {snap['waiting_terminals']}")

# 4. Seen-Set Dedup: replay ev-1 (should be no-op).
dashboard.handle_frame(AGUI_STEP_STARTED, {"terminal_id": "t1"}, event_id="ev-1")

# 5. Assertions.
hierarchy = dashboard.hierarchy()
assert "session-alpha" in hierarchy, "session-alpha must be in hierarchy"
assert hierarchy["session-alpha"]["terminal_count"] == 2, "session-alpha has 2 terminals"
assert snap["active_sessions"] == 1, "Only 1 session still active"
assert snap["by_provider"]["claude_code"] == 1, "1 claude_code terminal"
assert snap["waiting_terminals"] == ["t2"], "t2 is waiting"

print("\n[supervisor-dashboard] PASS: all assertions passed.")
sys.exit(0)
PYTHON

echo "[supervisor-dashboard] Done." >&2
