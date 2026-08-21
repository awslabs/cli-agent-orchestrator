#!/usr/bin/env bash
#
# MultiAgentSessionTimeline example.
#
# Demonstrates the fold-based session timeline construct by feeding synthetic
# AG-UI frames directly (no live server required). This dogfoods the library's
# own classes: RecordingUiEmitter + MultiAgentSessionTimeline.
#
# Usage:
#   ./examples/ag-ui/ag-ui-session-timeline/run.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

cleanup() {
    local code=$?
    exit "${code}"
}
trap cleanup EXIT INT TERM

echo "[session-timeline] Running MultiAgentSessionTimeline example..." >&2

uv run python3 - <<'PYTHON'
"""Demonstrate MultiAgentSessionTimeline fold composition."""
import json
import sys

from cli_agent_orchestrator.services.agui import (
    RecordingUiEmitter,
    MultiAgentSessionTimeline,
    TimelineEntry,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_TOOL_CALL_START,
    AGUI_TOOL_CALL_END,
    AGUI_TOOL_CALL_RESULT,
    AGUI_TEXT_MESSAGE_CONTENT,
)

emitter = RecordingUiEmitter()
timeline = MultiAgentSessionTimeline(emitter)

# 1. Open a delegation (supervisor -> worker-1).
delegation_frame = {
    "tool_call_id": "tc-delegate-1",
    "tool_call_name": "handoff",
    "timestamp": "2025-01-15T10:00:00Z",
    "metadata": {
        "sender": "supervisor",
        "receiver": "worker-1",
    },
}
timeline.handle_frame(AGUI_TOOL_CALL_START, delegation_frame, event_id="ev-1")
print("[1] After TOOL_CALL_START (delegation open):")
entries = timeline.entries()
print(f"    entries count = {len(entries)}")
print(f"    entry[0] = kind={entries[0].kind}, status={entries[0].status}, sender={entries[0].sender}")

# 2. A message from worker-1 back to supervisor.
message_frame = {
    "timestamp": "2025-01-15T10:01:00Z",
    "metadata": {
        "sender": "worker-1",
        "receiver": "supervisor",
    },
}
timeline.handle_frame(AGUI_TEXT_MESSAGE_CONTENT, message_frame, event_id="ev-2")
print("\n[2] After TEXT_MESSAGE_CONTENT (message entry):")
entries = timeline.entries()
print(f"    entries count = {len(entries)}")
msg_entry = [e for e in entries if e.kind == "message"][0]
print(f"    message: sender={msg_entry.sender}, receiver={msg_entry.receiver}")

# 3. Close the delegation with a TOOL_CALL_END.
close_frame = {
    "tool_call_id": "tc-delegate-1",
    "timestamp": "2025-01-15T10:02:00Z",
    "metadata": {},
}
timeline.handle_frame(AGUI_TOOL_CALL_END, close_frame, event_id="ev-3")
print("\n[3] After TOOL_CALL_END (delegation closed):")
entries = timeline.entries()
deleg = [e for e in entries if e.kind == "delegation"][0]
print(f"    delegation: status={deleg.status}, ended_at={deleg.ended_at}")

# 4. Open a second delegation, close with TOOL_CALL_RESULT (marks completed).
timeline.handle_frame(
    AGUI_TOOL_CALL_START,
    {
        "tool_call_id": "tc-a2a-1",
        "tool_call_name": "a2a_delegation",
        "timestamp": "2025-01-15T10:03:00Z",
        "metadata": {"sender": "supervisor", "receiver": "external-agent"},
    },
    event_id="ev-4",
)
timeline.handle_frame(
    AGUI_TOOL_CALL_RESULT,
    {
        "tool_call_id": "tc-a2a-1",
        "timestamp": "2025-01-15T10:04:00Z",
        "metadata": {},
    },
    event_id="ev-5",
)
print("\n[4] After a2a_delegation open+close:")
entries = timeline.entries()
a2a = [e for e in entries if e.id == "tc-a2a-1"][0]
print(f"    a2a: status={a2a.status}, tool_call_name={a2a.tool_call_name}")

# 5. Seen-Set Dedup: replay ev-1 (should be no-op).
timeline.handle_frame(AGUI_TOOL_CALL_START, delegation_frame, event_id="ev-1")

# 6. Projection.
proj = timeline.projection()
print(f"\n[5] projection() = {json.dumps(proj, indent=2)}")

# Assertions.
assert len(entries) == 3, f"Expected 3 entries, got {len(entries)}"
delegations = [e for e in entries if e.kind == "delegation"]
messages = [e for e in entries if e.kind == "message"]
assert len(delegations) == 2, "2 delegations"
assert len(messages) == 1, "1 message"
assert delegations[0].status == "completed", "First delegation completed"
assert delegations[1].status == "completed", "Second delegation completed"
assert messages[0].sender == "worker-1", "Message from worker-1"

print("\n[session-timeline] PASS: all assertions passed.")
sys.exit(0)
PYTHON

echo "[session-timeline] Done." >&2
